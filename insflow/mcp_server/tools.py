"""MCP Server 共享工具层（stdio 与 HTTP 两种传输共用）

工具清单（架构文档 §9.2，12 个）：
list_insights / get_insight / search_insights / run_diagnosis / ask_analyst /
list_competitors / get_competitor_timeline / get_maturity /
list_monitors / trigger_playbook / get_feedback_stats / propose_action

写工具策略：propose_action 只**起草待审批动作**（不派发），人工在控制台点批准；
trigger_playbook / run_diagnosis 为执行类，HTTP 端要求 write scope。
"""

import json

from ..agent import InsightAgent
from ..core.store import get_store


def _d(entity) -> dict:
    """pydantic 实体 → dict"""
    return entity.model_dump(mode="json")


async def tool_list_insights(workspace_id: str, status: str | None = None,
                             severity: str | None = None, limit: int = 20) -> dict:
    store = await get_store()
    insights = await store.list_insights(workspace_id, status=status, severity=severity, limit=limit)
    return {"insights": [_d(i) for i in insights], "total": len(insights)}


async def tool_get_insight(insight_id: str) -> dict:
    store = await get_store()
    ins = await store.get_insight(insight_id)
    return _d(ins) if ins else {"error": f"Insight not found: {insight_id}"}


async def tool_search_insights(workspace_id: str, query: str, limit: int = 8) -> dict:
    """语义检索洞察（本地向量；词面不命中也能召回）"""
    from ..engine.semantic_index import search_insights
    return await search_insights(workspace_id, query, limit=limit)


async def tool_run_diagnosis(workspace_id: str) -> dict:
    """触发一次全量诊断（同步）"""
    from ..engine.diagnosis import DiagnosisEngine
    engine = DiagnosisEngine(workspace_id)
    return await engine.run()


async def tool_ask_analyst(workspace_id: str, question: str) -> dict:
    """数据洞察问答（Agent，含引用溯源）"""
    agent = InsightAgent(workspace_id)
    return await agent.ask(question)


async def tool_list_competitors(workspace_id: str) -> dict:
    """列出竞品档案（从 competitor 类洞察聚合）"""
    store = await get_store()
    insights = await store.list_insights(workspace_id, limit=200)
    competitors: dict[str, dict] = {}
    for ins in insights:
        for ev in (ins.evidence_json or []):
            if not isinstance(ev, dict):
                continue
            cid = ev.get("competitor") or ev.get("target") or ""
            if not cid:
                continue
            entry = competitors.setdefault(cid, {
                "id": cid, "insight_count": 0,
                "types": [], "last_signal": "",
            })
            entry["insight_count"] += 1
            if ins.type not in entry["types"]:
                entry["types"].append(ins.type)
            ts = ins.created_at.isoformat() if ins.created_at else ""
            if ts > entry["last_signal"]:
                entry["last_signal"] = ts
    return {"competitors": list(competitors.values()), "total": len(competitors)}


async def tool_get_competitor_timeline(workspace_id: str, competitor: str) -> dict:
    """竞品时间线：该竞品相关的洞察按时间排序"""
    store = await get_store()
    insights = await store.list_insights(workspace_id, limit=200)
    timeline = []
    for ins in insights:
        evs = ins.evidence_json or []
        hit = any(
            isinstance(e, dict) and (e.get("competitor") == competitor or e.get("target") == competitor)
            for e in evs
        ) or competitor in (ins.title or "")
        if hit:
            timeline.append({
                "ts": ins.created_at.isoformat() if ins.created_at else "",
                "insight_id": ins.id,
                "type": ins.type,
                "title": ins.title,
                "severity": ins.severity.value,
            })
    return {"competitor": competitor, "timeline": timeline}


async def tool_get_maturity(workspace_id: str) -> dict:
    store = await get_store()
    ws = await store.get_workspace(workspace_id)
    if not ws:
        return {"error": f"Workspace not found: {workspace_id}"}
    return {
        "workspace_id": workspace_id,
        "stage": ws.stage.value,
        "maturity_level": ws.maturity_level.value,
    }


async def tool_list_monitors(workspace_id: str) -> dict:
    store = await get_store()
    rows = await store._fetchall(
        "SELECT id, kind, schedule_cron, last_run_at, state FROM monitors WHERE workspace_id = ?",
        (workspace_id,)
    )
    return {"monitors": rows, "total": len(rows)}


async def tool_trigger_playbook(workspace_id: str, insight_id: str, action_type: str,
                                target_ref: str | None = None) -> dict:
    """触发洞察的推荐动作（经 Action Router，结果可被验证状态机追踪）"""
    from ..actions.router import ActionContext, get_action_router
    store = await get_store()
    ins = await store.get_insight(insight_id)
    if not ins:
        return {"error": f"Insight not found: {insight_id}"}

    router = get_action_router()
    result = await router.dispatch(
        {
            "action_type": action_type,
            "target_ref": target_ref,
            "title": ins.title,
            "summary": ins.summary,
            "severity": ins.severity.value,
            "confidence": ins.confidence,
        },
        ActionContext(workspace_id=workspace_id, insight_id=insight_id),
    )
    return result


async def tool_get_feedback_stats(workspace_id: str) -> dict:
    store = await get_store()
    return await store.get_feedback_stats(workspace_id)


async def tool_propose_action(workspace_id: str, insight_id: str, action_type: str,
                              target_ref: str = "", rationale: str = "",
                              params: dict | None = None) -> dict:
    """起草待审批动作（进 pending + 通知；人批准后才派发）"""
    from ..engine.proposals import propose_action
    return await propose_action(
        workspace_id, insight_id=insight_id, action_type=action_type,
        target_ref=target_ref, params=params or {}, rationale=rationale,
        proposed_by="mcp")


# ========== MCP schema（OpenAI/MCP 通用 inputSchema）==========

MCP_TOOLS_SCHEMA = [
    {
        "name": "list_insights",
        "description": "列出洞察流，可按状态/严重程度过滤。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string"},
                "status": {"type": "string", "enum": ["new", "acknowledged", "actioned", "verified", "dismissed"]},
                "severity": {"type": "string", "enum": ["critical", "high", "medium", "low", "info"]},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["workspace_id"],
        },
    },
    {
        "name": "get_insight",
        "description": "获取洞察详情（证据链 + 推荐动作）。",
        "inputSchema": {
            "type": "object",
            "properties": {"insight_id": {"type": "string"}},
            "required": ["insight_id"],
        },
    },
    {
        "name": "search_insights",
        "description": "语义检索洞察（按意思找，词面不命中也能召回；本地向量，无需外部服务）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string"},
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 8},
            },
            "required": ["workspace_id", "query"],
        },
    },
    {
        "name": "run_diagnosis",
        "description": "触发一次全量流量诊断（AARRR + 异常检测 + 洞察生成 + 报告落盘）。",
        "inputSchema": {
            "type": "object",
            "properties": {"workspace_id": {"type": "string"}},
            "required": ["workspace_id"],
        },
    },
    {
        "name": "ask_analyst",
        "description": "向数据洞察分析师提问（自动工具调用 + 引用溯源）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string"},
                "question": {"type": "string"},
            },
            "required": ["workspace_id", "question"],
        },
    },
    {
        "name": "list_competitors",
        "description": "列出竞品档案（从竞品类洞察聚合）。",
        "inputSchema": {
            "type": "object",
            "properties": {"workspace_id": {"type": "string"}},
            "required": ["workspace_id"],
        },
    },
    {
        "name": "get_competitor_timeline",
        "description": "查询竞品异动时间线。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string"},
                "competitor": {"type": "string"},
            },
            "required": ["workspace_id", "competitor"],
        },
    },
    {
        "name": "get_maturity",
        "description": "获取工作区的增长阶段与数据成熟度等级。",
        "inputSchema": {
            "type": "object",
            "properties": {"workspace_id": {"type": "string"}},
            "required": ["workspace_id"],
        },
    },
    {
        "name": "list_monitors",
        "description": "列出监控任务（kind/cron/状态）。",
        "inputSchema": {
            "type": "object",
            "properties": {"workspace_id": {"type": "string"}},
            "required": ["workspace_id"],
        },
    },
    {
        "name": "trigger_playbook",
        "description": "触发洞察的推荐动作（Action Router 派发，目标系统执行）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string"},
                "insight_id": {"type": "string"},
                "action_type": {"type": "string",
                                 "description": "openflow.webhook_insight | mflow.create_content | mflow.register_topic | webhook.generic | feishu.notify | slack.notify"},
                "target_ref": {"type": "string"},
            },
            "required": ["workspace_id", "insight_id", "action_type"],
        },
    },
    {
        "name": "get_feedback_stats",
        "description": "查询动作验证效果统计（北极星指标）。",
        "inputSchema": {
            "type": "object",
            "properties": {"workspace_id": {"type": "string"}},
            "required": ["workspace_id"],
        },
    },
    {
        "name": "propose_action",
        "description": ("起草一个待人工审批的动作（不会立即执行；人在控制台批准后"
                        "才派发并进入 14 天验证）。适合让 Agent/上游系统提建议。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string"},
                "insight_id": {"type": "string"},
                "action_type": {"type": "string",
                                 "description": "feishu.notify | slack.notify | email.send | webhook.generic | mflow.create_content | openflow.webhook_insight 等"},
                "target_ref": {"type": "string"},
                "rationale": {"type": "string"},
                "params": {"type": "object"},
            },
            "required": ["workspace_id", "insight_id", "action_type"],
        },
    },
]

TOOL_IMPLS = {
    "list_insights": tool_list_insights,
    "get_insight": tool_get_insight,
    "search_insights": tool_search_insights,
    "run_diagnosis": tool_run_diagnosis,
    "ask_analyst": tool_ask_analyst,
    "list_competitors": tool_list_competitors,
    "get_competitor_timeline": tool_get_competitor_timeline,
    "get_maturity": tool_get_maturity,
    "list_monitors": tool_list_monitors,
    "trigger_playbook": tool_trigger_playbook,
    "get_feedback_stats": tool_get_feedback_stats,
    "propose_action": tool_propose_action,
}


async def call_mcp_tool(name: str, arguments: dict) -> str:
    """统一工具调用入口（供 stdio server 与 HTTP 端点共用）"""
    impl = TOOL_IMPLS.get(name)
    if not impl:
        return json.dumps({"error": f"Unknown tool: {name}"}, ensure_ascii=False)
    try:
        result = await impl(**arguments)
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)
