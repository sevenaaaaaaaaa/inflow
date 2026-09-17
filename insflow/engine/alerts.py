"""阈值告警规则 + 路由升级（对标 Grafana 告警的"够用版"）

能力：
- 规则：metric + 比较符 + 阈值 + 窗口 + 维度过滤 + 通知路由 + 升级策略
- 评估：读窗口聚合值 → 命中即产出一条洞察 + 按路由通知（复用 notify）
- 升级：命中后 N 小时未处理（无动作）则升级到 escalation.to

失败策略：单条规则出错不影响其他规则（逐条 try）。
"""

from datetime import UTC, datetime, timedelta

from ..core.files import EventBus
from ..core.store import get_store

OPS = {
    "gt": lambda v, t: v > t,
    "gte": lambda v, t: v >= t,
    "lt": lambda v, t: v < t,
    "lte": lambda v, t: v <= t,
    "abs_gt": lambda v, t: abs(v) > t,
}


async def evaluate_workspace(workspace_id: str) -> list[dict]:
    """评估该工作区所有启用规则，返回命中列表"""
    store = await get_store()
    rules = await store.list_alert_rules(workspace_id, enabled_only=True)
    fired: list[dict] = []
    for rule in rules:
        try:
            result = await _evaluate_rule(store, workspace_id, rule)
        except Exception as e:                     # 单条失败不影响其它规则
            fired.append({"rule_id": rule.get("id"), "error": str(e)})
            continue
        if result:
            fired.append(result)
    return fired


async def _evaluate_rule(store, workspace_id: str, rule: dict) -> dict | None:
    import json as _json
    op = str(rule.get("op") or "gt").lower()
    if op not in OPS:
        return None
    dims = {}
    try:
        dims = _json.loads(rule.get("dims_json") or "{}") or {}
    except Exception:
        dims = {}
    window = float(rule.get("window_days") or 7)
    value = await store.metric_total(workspace_id, rule["metric"], days=window,
                                     dim_filters=dims)
    threshold = float(rule.get("threshold") or 0)
    if not OPS[op](value, threshold):
        return None

    # 去重：同一规则同一窗口内不重复告警（last_fired_at 在窗口内则跳过）
    last = str(rule.get("last_fired_at") or "")
    if last:
        try:
            if datetime.fromisoformat(last) > datetime.now(UTC) - timedelta(days=window):
                return None
        except ValueError:
            pass

    routes = _json.loads(rule.get("routes_json") or "[]") or []
    escalation = _json.loads(rule.get("escalation_json") or "{}") or {}
    title = f"阈值告警：{rule.get('name') or rule['metric']}"
    summary = (f"{rule['metric']} 近 {window:g} 天为 {value:.4g}"
               f"（规则 {op} {threshold:g}）")
    insight = None
    try:
        from ..core.entities import Insight
        insight = await store.create_insight(Insight(
            workspace_id=workspace_id, type="threshold_alert", title=title,
            summary=summary, severity="high",
            evidence_json=[{"rule_id": rule.get("id"), "metric": rule["metric"],
                            "value": value, "threshold": threshold, "op": op,
                            "window_days": window, "routes": routes,
                            "escalation": escalation, "demo": False}]))
    except Exception:
        insight = None

    await store.mark_rule_fired(workspace_id, rule["id"])
    EventBus(workspace_id).emit("alert.threshold", {
        "rule_id": rule.get("id"), "metric": rule["metric"], "value": value,
        "threshold": threshold, "op": op, "routes": routes,
        "insight_id": getattr(insight, "id", "")})

    notified = await _notify(workspace_id, title, summary, routes, escalation)
    return {"rule_id": rule.get("id"), "name": rule.get("name"), "metric": rule["metric"],
            "value": value, "threshold": threshold, "op": op,
            "insight_id": getattr(insight, "id", ""), "notified": notified,
            "escalation": escalation, "routes": routes}


async def _notify(workspace_id: str, title: str, summary: str,
                  routes: list, escalation: dict) -> dict:
    """按路由通知：复用 ActionRouter（与订阅同一套通道实现）

    routes 元素可以是字符串（通道名，target 取工作区默认）或
    {"channel": "webhook", "target": {...}} 形式（精细指定地址）。
    """
    out: dict = {}
    try:
        from ..engine.router import ActionContext, get_action_router
        router = get_action_router()
    except Exception:
        return {"skipped": "动作路由不可用"}
    store = await get_store()
    ws = await store.get_workspace(workspace_id)
    defaults = dict(((ws.settings_json or {}).get("alert_targets") or {})) if ws else {}
    for item in routes[:5]:
        if isinstance(item, str):
            channel, target = item, defaults.get(item, {})
        elif isinstance(item, dict):
            channel = str(item.get("channel") or "")
            target = item.get("target") or defaults.get(channel, {})
        else:
            continue
        base = {"title": title, "summary": summary, "severity": "high"}
        if channel == "feishu":
            act = {**base, "action_type": "feishu.notify",
                   "target_ref": target.get("feishu_url", "")}
        elif channel == "slack":
            act = {**base, "action_type": "slack.notify",
                   "target_ref": target.get("slack_url", "")}
        elif channel == "email":
            act = {**base, "action_type": "email.send",
                   "target_ref": target.get("email_to", ""),
                   "params_json": {"to": target.get("email_to", ""),
                                   "smtp_host": target.get("smtp_host", ""),
                                   "smtp_port": target.get("smtp_port", ""),
                                   "smtp_from": target.get("smtp_from", "")}}
        elif channel == "webhook":
            act = {**base, "action_type": "webhook.generic",
                   "target_ref": target.get("webhook_url", ""),
                   "params_json": {"secret": target.get("webhook_secret", ""),
                                   "data": {"title": title, "summary": summary,
                                            "kind": "threshold_alert"}}}
        else:
            out[channel] = "未知通道"
            continue
        try:
            res = await router.dispatch(act, ActionContext(workspace_id=workspace_id))
            out[channel] = "ok" if res.get("ok") else res.get("error", "失败")
        except Exception as e:
            out[channel] = f"失败：{e}"
    if escalation and escalation.get("to"):
        out["escalation_to"] = str(escalation["to"])
    return out


async def sweep_escalations(workspace_id: str) -> list[dict]:
    """升级检查：命中告警后 N 小时仍无处理动作 → 再通知升级对象"""
    import json as _json
    store = await get_store()
    insights = await store.list_insights(workspace_id, limit=100)
    actions = await store.list_actions(workspace_id)
    handled_insight_ids = {a.insight_id for a in actions if a.insight_id}
    out = []
    for ins in insights:
        if ins.type != "threshold_alert":
            continue
        ev = (ins.evidence_json or [{}])[0]
        escalation = ev.get("escalation") or {}
        hours = float(escalation.get("after_hours") or 24)
        if ins.id in handled_insight_ids:
            continue
        if ins.created_at > datetime.now(UTC) - timedelta(hours=hours):
            continue
        target = escalation.get("to")
        if not target:
            continue
        out.append({"insight_id": ins.id, "escalated_to": target,
                    "age_hours": round((datetime.now(UTC) - ins.created_at)
                                       .total_seconds() / 3600, 1)})
        EventBus(workspace_id).emit("alert.escalated", out[-1])
    return out
