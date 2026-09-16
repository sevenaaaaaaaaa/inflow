"""Insight Flow 可观测性（R2）

1. 每日运行摘要：events.jsonl 聚合最近 24h 采集/洞察/验证/失败 → Markdown → 飞书推送
2. 告警分级出站：订阅关键失败事件 → 自动触发 feishu.notify（critical 加急）

通知目标由环境变量配置：INSFLOW_DAILY_WEBHOOK_URL / INSFLOW_ALERT_WEBHOOK_URL
"""

from datetime import datetime, timedelta, timezone

from ..actions.router import ActionContext, get_action_router
from ..core.files import EventBus

# 订阅的告警事件（分级）
ALERT_RULES = {
    "quota.exceeded": ("critical", "配额熔断"),
    "source.token_refresh_failed": ("critical", "授权轮换失败"),
    "monitor.error": ("high", "监控任务失败"),
    "action.dead": ("high", "动作重试耗尽"),
    "action.failed": ("medium", "动作执行失败"),
    "insight.quality_gate_blocked": ("medium", "洞察被质量门拦截"),
}


class DailyDigestBuilder:
    """每日运行摘要（无人值守）"""

    def __init__(self, workspace_id: str):
        self.workspace_id = workspace_id
        self.bus = EventBus(workspace_id)

    async def build(self, window_hours: int = 24) -> dict:
        """聚合最近 N 小时的运行数据 → 摘要结构 + Markdown"""
        since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
        events = self.bus.read(limit=2000)

        window = []
        for e in events:
            try:
                if datetime.fromisoformat(e["ts"]) >= since:
                    window.append(e)
            except (KeyError, ValueError):
                continue

        collect_ok = sum(1 for e in window if e["type"] == "monitor.run_finished")
        collect_fail = sum(1 for e in window if e["type"] == "monitor.alert")
        collect_total = collect_ok + collect_fail
        success_rate = (collect_ok / collect_total) if collect_total else None

        insights = sum(1 for e in window if e["type"] == "insight.created")
        blocked = sum(1 for e in window if e["type"] == "insight.quality_gate_blocked")
        verified = sum(1 for e in window if e["type"] == "feedback.received")
        token_rotations = sum(1 for e in window if e["type"] == "source.token_refreshed")
        token_failed = sum(1 for e in window if e["type"] == "source.token_refresh_failed")

        alerts = [{"level": lvl, "kind": kind, "payload": e.get("payload", {})}
                  for e in window if e["type"] in ALERT_RULES
                  for lvl, kind in [ALERT_RULES[e["type"]]]]

        md = self._render(window_hours, success_rate, collect_ok, collect_fail,
                          insights, blocked, verified, token_failed, len(alerts))

        summary = {
            "window_hours": window_hours,
            "collect_ok": collect_ok,
            "collect_fail": collect_fail,
            "success_rate": success_rate,
            "insights_created": insights,
            "quality_blocked": blocked,
            "feedback_received": verified,
            "token_rotations": token_rotations,
            "alerts": alerts,
            "alerts_count": len(alerts),
        }
        self.bus.emit("report.ready", {"kind": "daily_digest", "alerts": len(alerts)})
        return {"markdown": md, "summary": summary}

    def _render(self, window_hours, success_rate, ok, fail, insights, blocked,
                verified, token_failed, alerts) -> str:
        now = datetime.now(timezone.utc)
        rate_str = f"{success_rate:.0%}" if success_rate is not None else "无采集"
        return "\n".join([
            "# Insight Flow 每日运行摘要",
            "",
            f"窗口：最近 {window_hours} 小时 · 生成于 {now.strftime('%m-%d %H:%M UTC')}",
            "",
            "| 指标 | 数值 |",
            "|---|---|",
            f"| 采集成功率 | **{rate_str}**（成功 {ok} / 失败 {fail}） |",
            f"| 新增洞察 | {insights} 条 |",
            f"| 质量门拦截 | {blocked} 条 |",
            f"| 动作验证结论 | {verified} 条 |",
            f"| 授权轮换失败 | {token_failed} 次 |",
            f"| 分级告警 | {alerts} 条 |",
            "",
        ])

    async def push_to_feishu(self) -> dict:
        """生成摘要 → 经 feishu.notify 适配器推送"""
        result = await self.build()
        summary = result["summary"]
        rate = summary["success_rate"]
        router = get_action_router()
        dispatch = await router.dispatch(
            {
                "action_type": "feishu.notify",
                "title": "每日运行摘要",
                "summary": (
                    f"采集成功率 {f'{rate:.0%}' if rate is not None else '无采集'} · "
                    f"新增洞察 {summary['insights_created']} 条 · "
                    f"验证结论 {summary['feedback_received']} 条 · "
                    f"告警 {summary['alerts_count']} 条"
                ),
                "severity": "info",
            },
            ActionContext(workspace_id=self.workspace_id, insight_id="daily-digest"),
        )
        return {**summary, "dispatch": dispatch}


class AlertDispatcher:
    """告警分级出站（R2-2）"""

    def __init__(self, workspace_id: str):
        self.workspace_id = workspace_id

    async def dispatch_alerts(self, alerts: list[dict]) -> dict:
        """告警事件 → feishu.notify（critical/high 自动加急）"""
        if not alerts:
            return {"ok": True, "dispatched": 0}
        router = get_action_router()
        dispatched = 0
        for alert in alerts:
            level, kind = alert["level"], alert["kind"]
            payload = alert.get("payload", {})
            detail = "; ".join(f"{k}={v}" for k, v in list(payload.items())[:4])
            result = await router.dispatch(
                {
                    "action_type": "feishu.notify",
                    "title": f"【{level.upper()}】{kind}",
                    "summary": detail or "见事件流",
                    "severity": "high" if level in ("critical", "high") else "medium",
                },
                ActionContext(workspace_id=self.workspace_id, insight_id="alert"),
            )
            if result.get("ok"):
                dispatched += 1
        return {"ok": True, "dispatched": dispatched, "total": len(alerts)}
