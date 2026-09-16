"""Insight Flow 启动引导：定时任务集成

服务启动时注册三类后台任务（调度持久化，无人值守）：
1. monitor 恢复：monitors 表 → 调度器（服务重启自动恢复）
2. 每日 09:00 UTC：到期动作验证评估（14 天窗口）+ 周一自动生成周报
3. quota.audit：每小时核对配额账本（超量告警出站）
"""

import logging

from ..core.scheduler import get_scheduler
from ..core.store import get_store

logger = logging.getLogger("insflow.bootstrap")


async def bootstrap_scheduled_jobs() -> dict:
    """注册全部定时任务（FastAPI lifespan 启动时调用一次）"""
    store = await get_store()
    scheduler = get_scheduler("default")

    registered = {"monitors_restored": 0, "jobs": []}

    # 1. 监控任务恢复（调度持久化）
    from ..engine.monitors import MonitorService
    for ws in await store.list_workspaces():
        svc = MonitorService(ws.id, scheduler=scheduler)
        n = await svc.restore_all()
        registered["monitors_restored"] += n

    # 2. 每日 09:00 UTC：到期动作验证评估
    async def _evaluate_feedback():
        from ..actions.feedback_tracker import FeedbackTracker
        for ws in await store.list_workspaces():
            tracker = FeedbackTracker(ws.id)
            results = await tracker.evaluate_due_actions()
            done = [r for r in results if r.get("evaluated")]
            if done:
                logger.info(f"验证评估完成: {ws.id} {len(done)} 个动作")

    scheduler.add_job("feedback.evaluate-daily", "0 9 * * *",
                      "feedback.evaluate", {})
    scheduler.register_handler("feedback.evaluate", _evaluate_feedback)
    registered["jobs"].append("feedback.evaluate@daily-09:00")

    # 3. 每周一 09:30 UTC：增长周报
    async def _weekly_report():
        from ..engine.weekly_report import WeeklyReportBuilder
        for ws in await store.list_workspaces():
            try:
                await WeeklyReportBuilder(ws.id).build()
            except Exception:
                logger.exception(f"周报生成失败: {ws.id}")

    scheduler.add_job("report.weekly", "30 9 * * 1", "report.weekly", {})
    scheduler.register_handler("report.weekly", _weekly_report)
    registered["jobs"].append("report.weekly@mon-09:30")

    # 4. 每小时：配额审计
    async def _quota_audit():
        from ..core.security import get_quota_ledger
        ledger = get_quota_ledger()
        for source_id, usage in ledger.get_all_usage().items():
            if usage["state"] == "open":
                from ..core.files import EventBus
                EventBus("default").emit("quota.exceeded", {
                    "source": source_id, "usage": usage,
                })

    scheduler.add_job("quota.audit", "0 * * * *", "quota.audit", {})
    scheduler.register_handler("quota.audit", _quota_audit)
    registered["jobs"].append("quota.audit@hourly")

    # 5. 每日 09:15：数据备份（R1-2，data/ 是客户资产）
    async def _daily_backup():
        from ..engine.backup import BackupManager
        mgr = BackupManager()
        result = mgr.run()
        mgr.prune(keep_days=30)
        EventBus("default").emit("backup.completed", {
            "backup_dir": result.get("backup_dir"),
            "duration_s": result.get("duration_s"),
        })

    scheduler.add_job("backup.daily", "15 9 * * *", "backup.daily", {})
    scheduler.register_handler("backup.daily", _daily_backup)
    registered["jobs"].append("backup.daily@daily-09:15")

    # 6. 每日 09:20：OAuth 授权过期审计（7 天预警，R2-3）
    async def _token_audit():
        from ..engine.token_manager import TokenManager
        for ws in await store.list_workspaces():
            TokenManager(ws.id).audit_all()

    scheduler.add_job("token.audit", "20 9 * * *", "token.audit", {})
    scheduler.register_handler("token.audit", _token_audit)
    registered["jobs"].append("token.audit@daily-09:20")

    scheduler.start()
    logger.info(f"Bootstrap 完成: {registered}")
    return registered
