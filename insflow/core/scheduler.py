"""Insight Flow 调度中心

单进程 APScheduler + 事件驱动依赖编排：
- 任务类型：monitor.run（采集）、model.run（模型评估）、report.build、
  action.dispatch、feedback.evaluate、quota.audit
- 依赖编排：采集完成 → 触发依赖该指标的模型 → 洞察产出 → 触发订阅的报告与告警
- 熔断：每个 source 实例的配额账本超阈值自动暂停并告警
- 日历守卫：非重要任务仅工作日执行（周六日/法定节假日跳过），且限定
  本地 00:00–06:00 夜间窗口，06:00 强制截止；重要任务（payload.important /
  target_json.important）不受限
"""

import asyncio
import logging
from collections.abc import Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .files import EventBus
from .work_calendar import is_workday, local_date, seconds_until_night_end

logger = logging.getLogger("insflow.scheduler")


# 日历守卫只覆盖「用户自动化任务」；系统级定时任务（备份/轮转/巡检等）
# 里有不少周任务跑在周末，误伤会导致静默停摆（如周日快照清理永远不执行）
GATED_TASK_TYPES = {"monitor.run", "agent.task.run"}


class Scheduler:
    """调度中心

    职责：
    1. cron 定时触发监控任务（monitor.run）
    2. 事件驱动编排（采集完成触发模型评估，模型产出触发报告/告警）
    3. 任务重试与失败记录
    """

    def __init__(self, workspace_id: str = "default"):
        self.workspace_id = workspace_id
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        self._handlers: dict[str, Callable] = {}
        self._running = False

    # ========== 任务处理器注册 ==========

    def register_handler(self, task_type: str, handler: Callable) -> None:
        """注册任务处理器

        task_type: monitor.run | model.run | report.build | action.dispatch |
                   feedback.evaluate | quota.audit
        handler: async fn(payload: dict) -> dict
        """
        self._handlers[task_type] = handler

    # ========== cron 定时任务 ==========

    def add_monitor_job(self, monitor_id: str, cron: str, payload: dict | None = None) -> None:
        """添加监控定时任务"""
        job_id = f"monitor.{monitor_id}"
        if self._scheduler.get_job(job_id):
            self._scheduler.remove_job(job_id)
        self._scheduler.add_job(
            self._run_monitor,
            CronTrigger.from_crontab(cron, timezone="UTC"),
            id=job_id,
            args=[monitor_id, payload or {}],
            replace_existing=True,
            max_instances=1,
            misfire_grace_time=300,
        )
        logger.info(f"Added monitor job: {monitor_id} cron={cron}")

    def add_job(self, job_id: str, cron: str, task_type: str, payload: dict | None = None) -> None:
        """添加通用定时任务"""
        if self._scheduler.get_job(job_id):
            self._scheduler.remove_job(job_id)
        self._scheduler.add_job(
            self._run_task,
            CronTrigger.from_crontab(cron, timezone="UTC"),
            id=job_id,
            args=[task_type, payload or {}],
            replace_existing=True,
            max_instances=1,
            misfire_grace_time=300,
        )
        logger.info(f"Added job: {job_id} cron={cron} type={task_type}")

    def remove_job(self, job_id: str) -> None:
        """移除任务"""
        if self._scheduler.get_job(job_id):
            self._scheduler.remove_job(job_id)

    def list_jobs(self) -> list[dict]:
        """列出所有任务"""
        jobs = []
        for job in self._scheduler.get_jobs():
            jobs.append({
                "id": job.id,
                "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
                "trigger": str(job.trigger),
            })
        return jobs

    # ========== 内部执行 ==========

    @staticmethod
    def _is_important(payload: dict) -> bool:
        """任务重要性：payload.important 或 target_json.important（监控任务）"""
        if payload.get("important"):
            return True
        target = payload.get("target_json") or {}
        return bool(target.get("important")) if isinstance(target, dict) else False

    def _guard(self, task_type: str, payload: dict) -> str | None:
        """执行前守卫（工作日 + 夜间窗口）；返回拒绝原因，None=放行

        只作用于用户自动化任务（GATED_TASK_TYPES）+ 显式 workday_only 的任务：
        - 重要任务（payload.important / target_json.important）：不受限
        - 非重要任务：仅工作日执行（周六日与法定节假日跳过），
          且固定在本地凌晨 00:00–06:00 窗口内跑（06:00 强制截止见 _run_* 的超时）
        """
        if task_type not in GATED_TASK_TYPES and not payload.get("workday_only"):
            return None
        if self._is_important(payload):
            return None
        if not is_workday(local_date()):
            return "non-workday"
        return None

    async def _run_monitor(self, monitor_id: str, payload: dict) -> None:
        """执行监控任务（含状态机 + 事件流 + 依赖编排）"""
        bus = EventBus(self.workspace_id)

        # 执行前守卫：非重要任务非工作日不执行（周六日与法定节假日）
        reason = self._guard(f"monitor.{monitor_id}", payload)
        if reason:
            bus.emit("monitor.skipped", {
                "monitor_id": monitor_id, "reason": reason,
                "local_date": local_date().isoformat(),
            })
            logger.info(f"Monitor skipped ({reason}): {monitor_id}")
            return

        bus.emit("monitor.run_started", {"monitor_id": monitor_id, **payload})

        handler = self._handlers.get("monitor.run")
        if not handler:
            logger.error("No handler registered for monitor.run")
            return

        important = self._is_important(payload)
        # 非重要任务夜间窗口强制截止（本地 06:00 前必须结束）
        timeout = None if important else seconds_until_night_end()
        try:
            coro = handler({"monitor_id": monitor_id, **payload})
            result = await (asyncio.wait_for(coro, timeout=timeout)
                            if timeout else coro)
            bus.emit("monitor.run_finished", {
                "monitor_id": monitor_id,
                "ok": True,
                "summary": result,
            })
            # 依赖编排：采集完成 → 触发模型评估
            await self._trigger_dependent_models(result)
        except TimeoutError:
            logger.warning(f"Monitor killed at night-window deadline: {monitor_id}")
            bus.emit("monitor.alert", {
                "monitor_id": monitor_id,
                "error": "夜间窗口 06:00 截止，任务被强制结束（非重要任务仅可在 00:00-06:00 运行）",
            })
        except Exception as e:
            logger.exception(f"Monitor run failed: {monitor_id}")
            bus.emit("monitor.alert", {
                "monitor_id": monitor_id,
                "error": str(e),
            })

    async def _trigger_dependent_models(self, collect_result: dict) -> None:
        """依赖编排：根据采集结果触发依赖这些指标的模型"""
        available_metrics = collect_result.get("kinds", [])
        if not available_metrics:
            return

        handler = self._handlers.get("model.run")
        if not handler:
            return

        try:
            model_result = await handler({"available_metrics": available_metrics})
            # 模型产出洞察后触发报告/告警
            insights_count = model_result.get("insights_created", 0) if model_result else 0
            if insights_count:
                bus = EventBus(self.workspace_id)
                bus.emit("report.build_triggered", {
                    "reason": "new_insights",
                    "count": insights_count,
                })
        except Exception:
            logger.exception("Model orchestration failed")

    async def _run_task(self, task_type: str, payload: dict) -> None:
        """执行通用任务"""
        handler = self._handlers.get(task_type)
        if not handler:
            logger.error(f"No handler registered for {task_type}")
            return
        bus = EventBus(self.workspace_id)

        # 执行前守卫：非重要任务非工作日不执行（系统级任务标记 important=True 不受限）
        reason = self._guard(task_type, payload)
        if reason:
            bus.emit("task.skipped", {
                "task_type": task_type, "reason": reason,
                "local_date": local_date().isoformat(),
            })
            logger.info(f"Task skipped ({reason}): {task_type}")
            return

        bus.emit(f"{task_type}.started", payload)
        gated = task_type in GATED_TASK_TYPES or bool(payload.get("workday_only"))
        important = self._is_important(payload)
        # 非重要自动化任务夜间窗口强制截止（本地 06:00 前必须结束）
        timeout = (None if important else seconds_until_night_end()) if gated else None
        try:
            coro = handler(payload)
            await (asyncio.wait_for(coro, timeout=timeout) if timeout else coro)
            bus.emit(f"{task_type}.finished", payload)
        except TimeoutError:
            logger.warning(f"Task killed at night-window deadline: {task_type}")
            bus.emit(f"{task_type}.failed", {
                "error": "夜间窗口 06:00 截止，任务被强制结束", **payload})
        except Exception:
            logger.exception(f"Task {task_type} failed")
            bus.emit(f"{task_type}.failed", {"error": "see logs", **payload})

    # ========== 生命周期 ==========

    def start(self) -> None:
        """启动调度器"""
        if not self._running:
            self._scheduler.start()
            self._running = True
            logger.info("Scheduler started")

    def shutdown(self) -> None:
        """停止调度器"""
        if self._running:
            self._scheduler.shutdown(wait=False)
            self._running = False
            logger.info("Scheduler stopped")


# 全局实例
_scheduler: Scheduler | None = None


def get_scheduler(workspace_id: str = "default") -> Scheduler:
    """获取全局调度器"""
    global _scheduler
    if _scheduler is None:
        _scheduler = Scheduler(workspace_id)
    return _scheduler
