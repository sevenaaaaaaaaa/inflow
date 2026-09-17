"""Insight Flow 洞察订阅推送（G-5）

超级个体最常用的消费方式：让洞察主动送达（而不是每天来翻控制台）。

- 订阅规则：渠道（飞书/Webhook/Slack）+ 过滤（严重度/类型前缀/关键字/置信度）+ 模式
- 即时模式（immediate）：洞察创建即推
- 每日模式（daily）：每日汇总推送（由日报任务触发，无状态——按 24h 窗口聚合）
- 复用 Action Router 的动作适配器（feishu.notify / webhook.generic / slack.notify），
  推送失败只记事件，绝不影响洞察创建。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from ..actions.router import ActionContext, get_action_router
from ..core.files import EventBus
from ..core.store import get_store, generate_id

CHANNELS = ("feishu", "slack", "webhook", "email")


class SubscriptionError(Exception):
    pass


def _matches(insight, filters: dict) -> bool:
    """过滤匹配：severity / type_prefix / query / min_confidence"""
    if not filters:
        return True
    sev = getattr(insight, "severity", None)
    sev_val = sev.value if hasattr(sev, "value") else str(sev)
    if filters.get("severity") and sev_val not in filters["severity"]:
        return False
    if filters.get("type_prefix"):
        prefixes = filters["type_prefix"]
        if not any(str(insight.type).startswith(p) for p in prefixes):
            return False
    if filters.get("query"):
        blob = f"{insight.title} {insight.summary}"
        if filters["query"] not in blob:
            return False
    if filters.get("min_confidence") is not None:
        if float(insight.confidence) < float(filters["min_confidence"]):
            return False
    return True


class SubscriptionService:
    """订阅 CRUD + 匹配 + 分发"""

    def __init__(self, workspace_id: str):
        self.workspace_id = workspace_id
        self.bus = EventBus(workspace_id)

    # ========== CRUD ==========

    async def create(self, name: str, channels: list[str], target: dict,
                     filters: dict | None = None, mode: str = "immediate") -> dict:
        bad = [c for c in channels if c not in CHANNELS]
        if bad:
            raise SubscriptionError(f"不支持的渠道: {bad}（可用: {CHANNELS}）")
        if not channels:
            raise SubscriptionError("至少选择一个推送渠道")
        if mode not in ("immediate", "daily"):
            raise SubscriptionError("mode 必须是 immediate 或 daily")
        validate_target(channels, target)

        store = await get_store()
        sid = generate_id()
        now = datetime.now(timezone.utc)
        await store._execute(
            """INSERT INTO subscriptions (id, workspace_id, name, channels_json,
               target_json, filters_json, mode, enabled, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)""",
            (sid, self.workspace_id, name, json.dumps(channels, ensure_ascii=False),
             json.dumps(target, ensure_ascii=False),
             json.dumps(filters or {}, ensure_ascii=False), mode, now.isoformat()),
        )
        await store._db.commit()
        self.bus.emit("subscription.created", {
            "subscription_id": sid, "name": name, "channels": channels, "mode": mode,
        })
        return await self.get(sid)

    async def get(self, subscription_id: str) -> dict | None:
        store = await get_store()
        row = await store._fetchone(
            "SELECT * FROM subscriptions WHERE workspace_id = ? AND id = ?",
            (self.workspace_id, subscription_id))
        return _parse(row) if row else None

    async def list(self) -> list[dict]:
        store = await get_store()
        rows = await store._fetchall(
            "SELECT * FROM subscriptions WHERE workspace_id = ? ORDER BY created_at DESC",
            (self.workspace_id,))
        return [_parse(r) for r in rows]

    async def delete(self, subscription_id: str) -> bool:
        store = await get_store()
        cur = await store._execute(
            "DELETE FROM subscriptions WHERE workspace_id = ? AND id = ?",
            (self.workspace_id, subscription_id))
        await store._db.commit()
        if cur.rowcount:
            self.bus.emit("subscription.deleted", {"subscription_id": subscription_id})
        return cur.rowcount > 0

    async def set_enabled(self, subscription_id: str, enabled: bool) -> bool:
        store = await get_store()
        cur = await store._execute(
            "UPDATE subscriptions SET enabled = ? WHERE workspace_id = ? AND id = ?",
            (1 if enabled else 0, self.workspace_id, subscription_id))
        await store._db.commit()
        return cur.rowcount > 0

    # ========== 分发 ==========

    async def dispatch(self, insight, subs: list[dict] | None = None) -> dict:
        """洞察 → 匹配的即时订阅 → 推送（失败只记事件）"""
        subscriptions = subs if subs is not None else await self.list()
        router = get_action_router()
        sent = 0
        failed = 0
        for sub in subscriptions:
            if not sub["enabled"] or sub["mode"] != "immediate":
                continue
            if not _matches(insight, sub["filters"]):
                continue
            for action in _build_actions(sub, insight, self.workspace_id):
                try:
                    result = await router.dispatch(
                        action,
                        ActionContext(workspace_id=self.workspace_id,
                                      insight_id=getattr(insight, "id", "")))
                    if result.get("ok"):
                        sent += 1
                    else:
                        failed += 1
                except Exception as e:  # 推送绝不阻断主流程
                    failed += 1
                    self.bus.emit("subscription.push_failed", {
                        "subscription_id": sub["id"], "channel": sub["channels"],
                        "error": f"{type(e).__name__}: {e}",
                    })
        if sent or failed:
            self.bus.emit("subscription.pushed", {
                "insight_id": getattr(insight, "id", ""), "sent": sent, "failed": failed,
            })
        return {"sent": sent, "failed": failed}

    async def create_metric(self, name: str, metric: str, channels: list[str],
                            target: dict, *, chart: str = "line", days: float = 30,
                            compare_prev: bool = True, notes: str = "") -> dict:
        """图表级订阅：把某个指标的图（+同环比）按期推送给客户/团队"""
        return await self.create(name, channels, target,
                                 filters={"kind": "metric_chart", "metric": metric,
                                          "chart": chart, "days": days,
                                          "compare_prev": compare_prev, "notes": notes},
                                 mode="daily")

    async def dispatch_metric_charts(self) -> dict:
        """所有 metric_chart 订阅 → 渲染图表 HTML → 推送"""
        from ..viz.charts import line_chart
        from ..viz.frame import datapanel
        store = await get_store()
        sent = failed = 0
        for sub in await self.list():
            if not sub["enabled"] or sub["filters"].get("kind") != "metric_chart":
                continue
            metric = str(sub["filters"].get("metric") or "")
            if not metric:
                continue
            try:
                days = float(sub["filters"].get("days") or 30)
                series = await store.metric_series(self.workspace_id, metric, days=days)
                prev = await store.metric_series(self.workspace_id, metric, days=days * 2)
                labels = [p["bucket"] for p in series]
                vals = [p["value"] for p in series]
                half = len(prev) // 2 if prev else 0
                chart_svg = line_chart(
                    [{"name": metric, "values": vals}], labels, as_area=True,
                    anomaly=True, forecast_periods=7,
                    compare={"name": "上期",
                             "series": [{"name": "上期",
                                         "values": [p["value"] for p in prev[:half]]}]}
                    if (sub["filters"].get("compare_prev") and half) else None)
                total = sum(vals)
                body = (f"<h3>{metric} · 近 {days:g} 天</h3>"
                        f"<p>合计 {total:,.0f}；数据点 {len(vals)}</p>{chart_svg}")
                title = f"[图表订阅] {sub['name']} · {metric}"
                router = get_action_router()
                for action in _build_actions(sub, _MetricInsight(title, body),
                                             self.workspace_id):
                    action["summary"] = body      # 通道侧渲染图表 HTML
                    try:
                        res = await router.dispatch(
                            action, ActionContext(workspace_id=self.workspace_id))
                        sent += 1 if res.get("ok") else 0
                        failed += 0 if res.get("ok") else 1
                    except Exception:
                        failed += 1
            except Exception as e:
                failed += 1
                self.bus.emit("subscription.push_failed",
                              {"subscription_id": sub["id"], "error": str(e)})
        return {"sent": sent, "failed": failed}

    async def dispatch_daily(self) -> dict:
        """每日模式：按最近 24h 匹配洞察聚合推送（无状态）"""
        store = await get_store()
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        insights = [i for i in await store.list_insights(self.workspace_id, limit=500)
                    if i.created_at >= since]
        router = get_action_router()
        total_sent = 0
        for sub in await self.list():
            if not sub["enabled"] or sub["mode"] != "daily":
                continue
            matched = [i for i in insights if _matches(i, sub["filters"])]
            if not matched:
                continue
            lines = [f"{i.severity.value.upper()} · {i.title}" for i in matched[:10]]
            summary = f"近 24 小时命中 {len(matched)} 条洞察：\n" + "\n".join(lines)
            for action in _build_actions(sub, _DailyDigestStub(summary), self.workspace_id):
                try:
                    r = await router.dispatch(action, ActionContext(
                        workspace_id=self.workspace_id, insight_id="daily-digest"))
                    if r.get("ok"):
                        total_sent += 1
                except Exception:
                    pass
        return {"sent": total_sent}


class _DailyDigestStub:
    """供 _build_actions 复用的最小洞察视图"""
    def __init__(self, summary: str):
        self.id = "daily-digest"
        self.title = "每日洞察汇总"
        self.summary = summary
        self.severity = "medium"
        self.confidence = 1.0
        self.type = "daily_digest"


class _MetricInsight:
    """把"图表订阅"适配成洞察形状（复用 _build_actions 的通道构造）"""

    def __init__(self, title: str, summary: str):
        self.id = ""
        self.type = "metric_chart"
        self.title = title
        self.summary = summary
        self.severity = "info"


def _build_actions(sub: dict, insight, workspace_id: str) -> list[dict]:
    """订阅 → 动作适配器入参"""
    target = sub["target"]
    actions = []
    for channel in sub["channels"]:
        if channel == "feishu":
            actions.append({
                "action_type": "feishu.notify",
                "target_ref": target.get("feishu_url", ""),
                "title": getattr(insight, "title", ""),
                "summary": getattr(insight, "summary", ""),
                "severity": getattr(insight, "severity", "medium"),
            })
        elif channel == "slack":
            actions.append({
                "action_type": "slack.notify",
                "target_ref": target.get("slack_url", ""),
                "title": getattr(insight, "title", ""),
                "summary": getattr(insight, "summary", ""),
            })
        elif channel == "email":
            actions.append({
                "action_type": "email.send",
                "target_ref": target.get("email_to", ""),
                "params_json": {"to": target.get("email_to", ""),
                                "smtp_host": target.get("smtp_host", ""),
                                "smtp_port": target.get("smtp_port", ""),
                                "smtp_from": target.get("smtp_from", "")},
                "title": getattr(insight, "title", ""),
                "summary": getattr(insight, "summary", ""),
                "severity": str(getattr(insight, "severity", "medium")),
            })
        elif channel == "webhook":
            actions.append({
                "action_type": "webhook.generic",
                "target_ref": target.get("webhook_url", ""),
                "params_json": {"secret": target.get("webhook_secret", ""),
                                "data": {"insight_id": getattr(insight, "id", ""),
                                         "type": getattr(insight, "type", ""),
                                         "severity": str(getattr(insight, "severity", ""))}},
                "title": getattr(insight, "title", ""),
                "summary": getattr(insight, "summary", ""),
            })
    return actions


def validate_target(channels: list[str], target: dict) -> None:
    if "feishu" in channels and not target.get("feishu_url"):
        raise SubscriptionError("feishu 渠道需要 target.feishu_url")
    if "slack" in channels and not target.get("slack_url"):
        raise SubscriptionError("slack 渠道需要 target.slack_url")
    if "webhook" in channels and not target.get("webhook_url"):
        raise SubscriptionError("webhook 渠道需要 target.webhook_url")
    if "email" in channels and not target.get("email_to"):
        raise SubscriptionError("email 渠道需要 target.email_to")


def _parse(row: dict) -> dict:
    row = dict(row)
    row["channels"] = json.loads(row.pop("channels_json") or "[]")
    row["target"] = json.loads(row.pop("target_json") or "{}")
    row["filters"] = json.loads(row.pop("filters_json") or "{}")
    row["enabled"] = bool(row["enabled"])
    return row


async def notify_new_insight(workspace_id: str, insight_id: str) -> dict:
    """洞察创建后调用：即时订阅分发（异常吞掉，绝不影响主流程）"""
    try:
        store = await get_store()
        insight = await store.get_insight(insight_id)
        if not insight:
            return {"sent": 0, "failed": 0}
        return await SubscriptionService(workspace_id).dispatch(insight)
    except Exception:
        return {"sent": 0, "failed": 0}
