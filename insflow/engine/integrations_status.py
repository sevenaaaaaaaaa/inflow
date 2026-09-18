"""系统互操作状态（把"哪条通道断了"变成一屏可见）

三条通道 + 两个方向：
- IF → OpenFlow：webhook（洞察落 CDP）/ automation（自动化触发）/ plugin_api（插件路由）
- IF → MFlow：create_content / register_topic
- OpenFlow/MFlow → IF：ingest（cdp_event/lead/order/content.published）
- IF ← OpenFlow：MCP 只读白名单
- IF → 通用生态：webhook.generic / 通知渠道

数据来源（全部是真实存在的数据，不额外埋点）：
- 配置：环境变量存在性（不回显密钥值）
- 动作：`actions` 表按类型/状态聚合 + `action_dead_letters` 待重放数
- 事件：事件流最近记录（ingest.received / ingest.duplicate / ingest.rejected / external.event）
"""

import os
from datetime import UTC, datetime

CHANNELS = [
    {"key": "openflow.inbound", "name": "IF → OpenFlow（洞察落 CDP）",
     "direction": "out", "action_types": ["openflow.webhook_insight"],
     "env": ["OPENFLOW_BASE_URL", "OPENFLOW_WEBHOOK_SECRET",
             "OPENFLOW_INBOUND_CONNECTOR_ID"]},
    {"key": "openflow.automation", "name": "IF → OpenFlow（自动化触发）",
     "direction": "out", "action_types": ["openflow.automation"],
     "env": ["OPENFLOW_BASE_URL", "OPENFLOW_WEBHOOK_SECRET"]},
    {"key": "openflow.plugin", "name": "IF → OpenFlow（插件路由）",
     "direction": "out", "action_types": ["openflow.plugin_api"],
     "env": ["OPENFLOW_BASE_URL", "OPENFLOW_PLUGIN_ROUTE"]},
    {"key": "mflow.content", "name": "IF → MFlow（触发产稿）",
     "direction": "out", "action_types": ["mflow.create_content",
                                          "mflow.register_topic"],
     "env": ["MFLOW_BASE_URL"]},
    {"key": "openflow.mcp", "name": "IF ← OpenFlow（MCP 只读）",
     "direction": "in", "action_types": [],
     "env": ["OPENFLOW_MCP_URL", "OPENFLOW_MCP_API_KEY"]},
    {"key": "ingest", "name": "OpenFlow/MFlow → IF（事件入站）",
     "direction": "in", "action_types": [],
     "env": ["INSFLOW_INGEST_SECRET"]},
]


async def status(workspace_id: str) -> dict:
    from ..core.store import get_store
    store = await get_store()
    rows = await store._fetchall(
        """SELECT action_type, state, COUNT(*) AS n FROM actions
           WHERE workspace_id = ? GROUP BY action_type, state""", (workspace_id,))
    by_type: dict[str, dict[str, int]] = {}
    for r in rows:
        by_type.setdefault(str(r["action_type"]), {})[str(r["state"])] = int(r["n"])
    pending_dead = await store.list_dead_letters(workspace_id, limit=500)
    dead_by_type: dict[str, int] = {}
    for letter in pending_dead:
        dead_by_type[str(letter["action_type"])] = dead_by_type.get(
            str(letter["action_type"]), 0) + 1

    # 事件流近况（最近 500 条里筛互操作相关）
    from ..core.files import EventBus
    events = EventBus(workspace_id).read(limit=500)
    rel = [e for e in events if str(e.get("type", "")).startswith(
        ("ingest.", "external.", "action.", "template.applied"))][-60:]

    channels = []
    for ch in CHANNELS:
        missing = [k for k in ch["env"] if not os.environ.get(k)]
        stats = {"total": 0, "states": {}, "dead_letters": 0}
        for at in ch["action_types"]:
            for state, n in (by_type.get(at) or {}).items():
                stats["states"][state] = stats["states"].get(state, 0) + n
                stats["total"] += n
            stats["dead_letters"] += dead_by_type.get(at, 0)
        related = [e for e in rel
                   if any(at.split(".")[0] in str(e.get("type", "")) or
                          at in str(e.get("payload", "")) for at in ch["action_types"])] \
            if ch["action_types"] else [e for e in rel
                                        if str(e.get("type", "")).startswith("ingest.")] \
            if ch["key"] == "ingest" else []
        last = related[-1] if related else None
        if missing:
            health = "unconfigured"
        elif stats["dead_letters"]:
            health = "degraded"
        else:
            health = "ok"
        channels.append({
            "key": ch["key"], "name": ch["name"], "direction": ch["direction"],
            "configured": not missing, "missing_env": missing, "health": health,
            "stats": stats,
            "last_event": ({"ts": last.get("ts"), "type": last.get("type")}
                           if last else None),
        })
    return {"workspace_id": workspace_id, "generated_at": datetime.now(UTC).isoformat(),
            "channels": channels,
            "events_recent": [{"ts": e.get("ts"), "type": e.get("type")}
                              for e in rel[-10:]][::-1]}
