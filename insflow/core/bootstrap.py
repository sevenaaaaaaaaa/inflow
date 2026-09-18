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


def _leader_wrapped(handler):
    """把调度 handler 包一层「仅 leader 执行」

    多实例部署时避免每个实例各跑一遍采集/汇总/告警；幂等窗口键是第二道保险。
    无 Redis（单机私有化）时 leader 恒为 True，行为与单机完全一致。
    """
    import functools

    @functools.wraps(handler)
    async def wrapper(payload=None):
        from ..core.leader import get_leader
        leader = get_leader()
        if not await leader.acquire():
            logger.info(f"非 leader（{leader.ident}），跳过任务 {handler.__name__}")
            return {"skipped": "not-leader"}
        return await handler(payload)

    return wrapper


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
    async def _evaluate_feedback(payload=None):
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
    async def _weekly_report(payload=None):
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
    async def _quota_audit(payload=None):
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
    async def _daily_backup(payload=None):
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

    # 6. 每日 09:35：每日运行摘要（R2-1）；09:20 的 OAuth 临期预警见下
    async def _digest_daily(payload=None):
        """每日运行摘要（采集成功率/洞察/验证/告警）→ 飞书（配置了 feishu 才发）"""
        from ..engine.observability import DailyDigestBuilder
        for ws in await store.list_workspaces():
            try:
                row = await store.get_workspace(ws.id)
                targets = ((row.settings_json or {}).get("alert_targets") or {}) if row else {}
                if not (targets.get("feishu") or {}).get("feishu_url"):
                    continue
                await DailyDigestBuilder(ws.id).push_to_feishu()
            except Exception:
                logger.exception(f"每日摘要推送失败: {ws.id}")

    scheduler.add_job("digest.daily", "35 9 * * *", "digest.daily", {})
    scheduler.register_handler("digest.daily", _digest_daily)
    registered["jobs"].append("digest.daily@daily-09:35")

    async def _pending_flush(payload=None):
        """静默/节流期累计的告警，恢复后合并补发（每工作区一条摘要）"""
        from ..core.files import EventBus
        from ..engine.alerts import _notify
        from ..engine.notify_policy import QuietHours, drain_pending, policy_of
        for ws in await store.list_workspaces():
            try:
                row = await store.get_workspace(ws.id)
                policy = policy_of(row.settings_json if row else {})
                if QuietHours(policy.get("quiet_hours")).active():
                    continue
                items = drain_pending(ws.id)
                if not items:
                    continue
                titles = [str(i["alert"].get("name") or i["alert"].get("metric"))
                          for i in items][:10]
                await _notify(ws.id, f"静默期累计 {len(items)} 条告警",
                              "、".join(titles), ["feishu", "webhook"], {})
                EventBus(ws.id).emit("alert.flushed", {"count": len(items)})
            except Exception:
                logger.exception(f"待发告警补发失败: {ws.id}")

    scheduler.add_job("alerts.flush", "*/15 * * * *", "alerts.flush", {})
    scheduler.register_handler("alerts.flush", _pending_flush)
    registered["jobs"].append("alerts.flush@every-15min")

    # 5.5 OAuth 临期预警（≤7 天，主动通知而非只写事件流）
    async def _token_audit(payload=None):
        from ..engine.alerts import _notify
        from ..engine.token_manager import TokenManager
        for ws in await store.list_workspaces():
            try:
                statuses = TokenManager(ws.id).audit_all()
                urgent = [s for s in statuses
                          if s["state"] in ("expiring_soon", "expired")]
                if not urgent:
                    continue
                detail = "、".join(
                    f"{s['provider']} {s['state']}"
                    + (f"（剩 {s['remaining_days']} 天）"
                       if s.get("remaining_days") is not None else "")
                    for s in urgent)
                await _notify(ws.id, "OAuth 授权即将到期",
                              detail + "；请重新授权以免采集中断",
                              ["feishu", "webhook", "email"], {})
            except Exception:
                logger.exception(f"token 预警失败: {ws.id}")

    scheduler.add_job("token.audit", "20 9 * * *", "token.audit", {})
    scheduler.register_handler("token.audit", _token_audit)
    registered["jobs"].append("token.audit@daily-09:20")

    # 6.5 每日 09:10：事件流轮转（性能守则：事件流不得无限增长）
    async def _event_rotate(payload=None):
        from ..core.files import EventBus
        kept_total = 0
        for ws in await store.list_workspaces():
            r = EventBus(ws.id).rotate(keep_days=30)
            kept_total += r["kept"]
        # WAL checkpoint：控制 -wal 膨胀（写多后 WAL 会显著大于主库）
        await store.wal_checkpoint("TRUNCATE")
        logger.info(f"事件流轮转 + WAL checkpoint 完成：保留 {kept_total} 行")

    scheduler.add_job("events.rotate", "10 9 * * *", "events.rotate", {})
    scheduler.register_handler("events.rotate", _event_rotate)
    registered["jobs"].append("events.rotate@daily-09:10")

    # 7. 每日 09:25：订阅每日汇总推送（G-5 daily 模式）
    async def _subscription_daily(payload=None):
        from ..engine.subscriptions import SubscriptionService
        for ws in await store.list_workspaces():
            try:
                svc = SubscriptionService(ws.id)
                await svc.dispatch_daily()
                await svc.dispatch_metric_charts()   # 图表级订阅
                await svc.dispatch_snapshots()       # 看板快照订阅
            except Exception:
                logger.exception(f"订阅日报推送失败: {ws.id}")

    scheduler.add_job("subscription.daily", "25 9 * * *", "subscription.daily", {})
    scheduler.register_handler("subscription.daily", _subscription_daily)
    registered["jobs"].append("subscription.daily@daily-09:25")


    # 8. 每日 09:05：预聚合汇总（metric_daily，长窗口查询提速）
    async def _rollup_daily(payload=None):
        from ..core.rollup import rollup
        for ws in await store.list_workspaces():
            try:
                await rollup(ws.id, days=120)
            except Exception:
                logger.exception(f"预聚合失败: {ws.id}")

    scheduler.add_job("rollup.daily", "5 9 * * *", "rollup.daily", {})
    scheduler.register_handler("rollup.daily", _rollup_daily)
    registered["jobs"].append("rollup.daily@daily-09:05")

    # 9. 每小时：阈值告警规则评估 + 升级检查
    async def _alerts_hourly(payload=None):
        from ..engine.alerts import evaluate_workspace, sweep_escalations
        for ws in await store.list_workspaces():
            try:
                fired = await evaluate_workspace(ws.id)
                if fired:
                    logger.info(f"阈值告警命中: {ws.id} x{len(fired)}")
                esc = await sweep_escalations(ws.id)
                if esc:
                    logger.info(f"告警升级: {ws.id} x{len(esc)}")
            except Exception:
                logger.exception(f"告警评估失败: {ws.id}")

    async def _dq_daily(payload=None):
        """每日数据质量体检：停更/缺口告警（命中才通知），可选自动续采"""
        from ..engine.data_quality import alert_stale, backfill
        for ws in await store.list_workspaces():
            try:
                out = await alert_stale(ws.id)
                row = await store.get_workspace(ws.id)
                auto = bool(((row.settings_json or {}).get("dq_auto_backfill")))
                if auto:
                    res = await backfill(ws.id, days=7)
                    if res["ran"]:
                        logger.info(f"自动续采: {ws.id} 执行 {len(res['ran'])} 个监控")
                if out.get("fired"):
                    logger.info(f"数据质量告警: {ws.id} x{out['fired']}")
            except Exception:
                logger.exception(f"数据质量体检失败: {ws.id}")

    async def _snapshot_cleanup(payload=None):
        """每周清理过期快照（默认保留 90 天）"""
        from ..engine.snapshot import cleanup
        for ws in await store.list_workspaces():
            try:
                out = cleanup(ws.id)
                if out["removed"]:
                    logger.info(f"快照清理: {ws.id} 删除 {out['removed']} 个")
            except Exception:
                logger.exception(f"快照清理失败: {ws.id}")

    scheduler.add_job("snapshot.cleanup", "0 4 * * 0", "snapshot.cleanup", {})
    scheduler.register_handler("snapshot.cleanup", _snapshot_cleanup)
    registered["jobs"].append("snapshot.cleanup@sun-04:00")

    async def _privacy_retention(payload=None):
        """每周留存清理（按工作区 retention 策略；无策略=默认值）"""
        from ..engine.privacy import retention_sweep
        for ws in await store.list_workspaces():
            try:
                res = await retention_sweep(ws.id, dry_run=False)
                deleted = sum(v.get("deleted", 0) for v in res["result"].values())
                if deleted:
                    logger.info(f"留存清理: {ws.id} 删除 {deleted} 行")
            except Exception:
                logger.exception(f"留存清理失败: {ws.id}")

    scheduler.add_job("privacy.retention", "30 3 * * 0", "privacy.retention", {})
    scheduler.register_handler("privacy.retention", _privacy_retention)
    registered["jobs"].append("privacy.retention@sun-03:30")

    scheduler.add_job("dq.daily", "45 9 * * *", "dq.daily", {})
    scheduler.register_handler("dq.daily", _dq_daily)
    registered["jobs"].append("dq.daily@daily-09:45")

    scheduler.add_job("alerts.hourly", "40 * * * *", "alerts.hourly", {})
    scheduler.register_handler("alerts.hourly", _alerts_hourly)
    registered["jobs"].append("alerts.hourly@hourly-40")

    # 多实例：定时任务只由 leader 执行（无 Redis → 单机恒为 leader）
    from ..core.leader import get_leader
    leader = get_leader()
    for task_type, handler in list(scheduler._handlers.items()):
        scheduler.register_handler(task_type, _leader_wrapped(handler))
    await leader.acquire()
    registered["leader"] = await leader.status()

    scheduler.start()
    logger.info(f"Bootstrap 完成: {registered}")
    return registered
