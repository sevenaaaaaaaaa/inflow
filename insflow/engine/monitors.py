"""Insight Flow 监控任务服务（M6）

统一管理监控任务的生命周期：
- CRUD（持久化到 monitors 表）
- 调度持久化（cron → Scheduler 注册，服务重启后可从 DB 恢复）
- 执行编排（按 kind 路由到对应引擎：site_change → ChangeMonitor 等）
"""

from datetime import UTC, datetime

from ..core.files import EventBus
from ..core.scheduler import Scheduler
from ..core.statemachine import MONITOR_MACHINE
from ..core.store import get_store
from .change_monitor import ChangeMonitor


class MonitorService:
    """监控任务 CRUD + 调度 + 执行"""

    # 支持的监控类型 → 引擎路由
    KINDS = ("site_change", "keyword", "brand_mention", "topic", "journey")

    def __init__(self, workspace_id: str, scheduler: Scheduler | None = None):
        self.workspace_id = workspace_id
        self.scheduler = scheduler or Scheduler(workspace_id)
        self.bus = EventBus(workspace_id)

    # ========== CRUD + 调度 ==========

    async def create(self, kind: str, target: dict, schedule_cron: str = "0 */6 * * *") -> dict:
        """创建监控任务并注册到调度器"""
        if kind not in self.KINDS:
            raise ValueError(f"未知监控类型: {kind}（支持: {self.KINDS}）")
        store = await get_store()
        monitor = await store.create_monitor(self.workspace_id, kind, target, schedule_cron)
        self._register_job(monitor)
        self.bus.emit("monitor.created", {"monitor_id": monitor["id"], "kind": kind})
        return monitor

    async def delete(self, monitor_id: str) -> bool:
        store = await get_store()
        ok = await store.delete_monitor(self.workspace_id, monitor_id)
        if ok:
            self.scheduler.remove_job(f"monitor.{monitor_id}")
            self.bus.emit("monitor.deleted", {"monitor_id": monitor_id})
        return ok

    async def list(self) -> list[dict]:
        store = await get_store()
        return await store.list_monitors_full(self.workspace_id)

    def _register_job(self, monitor: dict) -> None:
        """监控 → 调度器 cron 注册（持久化于 DB，服务启动时 restore_all）"""
        self.scheduler.add_monitor_job(
            monitor["id"], monitor["schedule_cron"],
            {"kind": monitor["kind"], "target": monitor["target_json"],
             "workspace_id": self.workspace_id},
        )

    async def restore_all(self) -> int:
        """服务启动：从 DB 恢复全部监控任务到调度器（调度持久化）"""
        monitors = await self.list()
        for m in monitors:
            self._register_job(m)
        return len(monitors)

    # ========== 执行（由调度器或手动触发）==========

    async def run(self, monitor_id: str) -> dict:
        """执行一次监控任务（含状态机 + 事件流）"""
        store = await get_store()
        monitor = await store.get_monitor(self.workspace_id, monitor_id)
        if not monitor:
            return {"error": f"Monitor not found: {monitor_id}"}

        MONITOR_MACHINE.validate(monitor["state"] if monitor["state"] != "running" else "idle",
                                 "running")
        await store.update_monitor_state(monitor_id, "running")
        self.bus.emit("monitor.run_started", {"monitor_id": monitor_id})

        try:
            result = await self._execute_by_kind(monitor)
            await store.update_monitor_state(monitor_id, "ok", last_run_at=datetime.now(UTC))
            self.bus.emit("monitor.run_finished", {
                "monitor_id": monitor_id, "kind": monitor["kind"],
                "summary": result,
            })
            return result
        except Exception as e:
            await store.update_monitor_state(monitor_id, "error",
                                             last_run_at=datetime.now(UTC))
            self.bus.emit("monitor.alert", {"monitor_id": monitor_id, "error": str(e)})
            return {"error": f"{type(e).__name__}: {e}"}

    async def _execute_by_kind(self, monitor: dict) -> dict:
        """按类型路由到执行引擎"""
        kind = monitor["kind"]
        target = monitor["target_json"]

        from .collector_router import CollectorRouter

        if kind == "site_change":
            url = target.get("url", "")
            if not url:
                raise ValueError("site_change 监控缺少 target.url")
            markdown = await self._fetch_page(target)
            engine = ChangeMonitor(self.workspace_id)
            return await engine.check(monitor["id"], url, current_markdown=markdown,
                                      cost=target.get("_cost"))

        router = CollectorRouter(self.workspace_id)
        if kind == "keyword":
            return await router.run_keyword(monitor["id"], target)
        if kind == "brand_mention":
            return await router.run_brand_mention(monitor["id"], target)
        if kind == "topic":
            return await router.run_topic(monitor["id"], target)
        if kind == "journey":
            return await router.run_journey(monitor["id"], target)
        raise ValueError(f"未知监控类型: {kind}")

    async def _fetch_page(self, target: dict) -> str:
        """经 Firecrawl 插件抓取页面（配额账本保护）"""
        from ..collectors.base import CollectContext
        from ..collectors.registry import get_registry

        registry = get_registry()
        plugin = registry.get("firecrawl")
        if not plugin:
            raise RuntimeError("firecrawl 插件未安装（无法抓取页面）")

        from ..core.security import get_quota_ledger, get_vault
        ledger = get_quota_ledger()
        breaker = ledger.get_breaker("firecrawl")
        breaker.check()
        vault = get_vault()
        api_key = vault.get("firecrawl_api_key") or target.get("api_key", "")

        ctx = CollectContext(
            workspace_id=self.workspace_id, monitor_id=target.get("monitor_id", ""),
            config={"api_key": api_key, "url": target["url"]},
        )
        result = await plugin.collect(ctx)
        breaker.record_call(1)
        self.bus.emit("source.collected", {
            "source": "firecrawl", "kind": result.kind,
            "cost": result.cost,
        })
        markdown = ""
        for item in result.items:
            markdown = item.get("markdown", "")
            break
        return markdown
