"""Insight Flow Agent 工具集

Agent 消费的确定性工具（全部只读，写操作走 Action Router 审批链路）：
- query_insights：按条件检索洞察流
- get_insight_detail：洞察详情（含 evidence 供引用溯源）
- list_models：列出可运行的洞察模型
- get_feedback_stats：动作验证效果统计（北极星）
- list_reports：报告中心目录
"""

import json

from ..core.files import ReportStore
from ..core.store import get_store


async def tool_query_insights(workspace_id: str, status: str | None = None,
                              severity: str | None = None,
                              insight_type: str | None = None, limit: int = 10) -> dict:
    """检索洞察流（Agent 主工具）"""
    store = await get_store()
    insights = await store.list_insights(workspace_id, status=status, severity=severity, limit=limit)
    if insight_type:
        insights = [i for i in insights if i.type == insight_type]
    return {
        "total": len(insights),
        "insights": [{
            "id": i.id,
            "type": i.type,
            "title": i.title,
            "summary": i.summary,
            "severity": i.severity.value,
            "confidence": i.confidence,
            "status": i.status.value,
            "created_at": i.created_at.isoformat(),
        } for i in insights],
    }


async def tool_get_insight_detail(insight_id: str) -> dict:
    """洞察详情（evidence / actions / models，引用溯源用）"""
    store = await get_store()
    ins = await store.get_insight(insight_id)
    if not ins:
        return {"error": f"Insight not found: {insight_id}"}
    return {
        "id": ins.id,
        "type": ins.type,
        "title": ins.title,
        "summary": ins.summary,
        "severity": ins.severity.value,
        "confidence": ins.confidence,
        "evidence": ins.evidence_json,
        "recommended_actions": [a.model_dump() for a in ins.actions_json],
        "models": ins.models_json,
        "status": ins.status.value,
        "created_at": ins.created_at.isoformat(),
    }


async def tool_list_models() -> dict:
    """列出内置模型库"""
    from ..engine.router import get_model_router
    return {"models": get_model_router().list_models()}


async def tool_get_feedback_stats(workspace_id: str) -> dict:
    """动作验证效果统计（effective/neutral/harmful 分布）"""
    store = await get_store()
    return await store.get_feedback_stats(workspace_id)


async def tool_list_reports(workspace_id: str) -> dict:
    """报告中心目录（诊断/竞品/成熟度/验证）"""
    store = ReportStore(workspace_id)
    return {
        "reports": {
            cat: [p.name for p in store.list_reports(cat)]
            for cat in ("diagnosis", "competitors", "maturity", "verification")
        },
    }


# 工具注册表：OpenAI function calling schema + 本地执行器
AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_insights",
            "description": "检索洞察流。支持按状态/严重程度/类型过滤。",
            "parameters": {
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "string"},
                    "status": {"type": "string", "enum": ["new", "acknowledged", "actioned", "verified", "dismissed"]},
                    "severity": {"type": "string", "enum": ["critical", "high", "medium", "low", "info"]},
                    "insight_type": {"type": "string"},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["workspace_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_insight_detail",
            "description": "获取洞察详情（含证据链与推荐动作，用于引用溯源）。",
            "parameters": {
                "type": "object",
                "properties": {"insight_id": {"type": "string"}},
                "required": ["insight_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_models",
            "description": "列出已注册的洞察模型及其所需指标。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_feedback_stats",
            "description": "查询动作验证效果统计（哪些洞察建议真的有效）。",
            "parameters": {
                "type": "object",
                "properties": {"workspace_id": {"type": "string"}},
                "required": ["workspace_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_reports",
            "description": "列出报告中心目录（诊断/竞品/成熟度/验证报告）。",
            "parameters": {
                "type": "object",
                "properties": {"workspace_id": {"type": "string"}},
                "required": ["workspace_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "metric_qa",
            "description": ("自然语言问数：指标数值/趋势/维度分布/数据质量/渠道归因/"
                            "动作增量（规则解析，无 LLM 也能用）。"),
            "parameters": {"type": "object", "properties": {
                "workspace_id": {"type": "string"}, "question": {"type": "string"}},
                "required": ["workspace_id", "question"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "narrate",
            "description": "指标自动叙事：结论/异常/趋势/维度贡献/数据质量/建议。",
            "parameters": {"type": "object", "properties": {
                "workspace_id": {"type": "string"}, "metric": {"type": "string"},
                "days": {"type": "number", "default": 30},
                "dim": {"type": "string"}},
                "required": ["workspace_id", "metric"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "data_quality",
            "description": "数据质量体检：新鲜度/完整性/缺口/续采方式。",
            "parameters": {"type": "object", "properties": {
                "workspace_id": {"type": "string"},
                "window_days": {"type": "integer", "default": 14}},
                "required": ["workspace_id"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "attribution",
            "description": "渠道归因（last_click/first_click/linear/time_decay/markov）。",
            "parameters": {"type": "object", "properties": {
                "workspace_id": {"type": "string"}, "days": {"type": "number", "default": 30},
                "method": {"type": "string", "default": "linear"}},
                "required": ["workspace_id"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "action_lift",
            "description": "动作增量：前后对比 + 自助法区间（非随机实验）。",
            "parameters": {"type": "object", "properties": {
                "workspace_id": {"type": "string"}, "days": {"type": "number", "default": 30}},
                "required": ["workspace_id"]},
        },
    },
]

TOOL_EXECUTORS = {
    "query_insights": lambda args: tool_query_insights(**args),
    "get_insight_detail": lambda args: tool_get_insight_detail(**args),
    "list_models": lambda args: tool_list_models(**args),
    "get_feedback_stats": lambda args: tool_get_feedback_stats(**args),
    "list_reports": lambda args: tool_list_reports(**args),
    "metric_qa": lambda args: tool_metric_qa(**args),
    "narrate": lambda args: tool_narrate(**args),
    "data_quality": lambda args: tool_data_quality(**args),
    "attribution": lambda args: tool_attribution(**args),
    "action_lift": lambda args: tool_action_lift(**args),
}


async def execute_tool(name: str, args: dict) -> str:
    """执行工具并返回 JSON 字符串（喂回 LLM）"""
    executor = TOOL_EXECUTORS.get(name)
    if not executor:
        return json.dumps({"error": f"Unknown tool: {name}"}, ensure_ascii=False)
    try:
        result = await executor(args)
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)

# ========== 语义层 / 数据质量 / 归因 / 叙事（Batch10） ==========

async def tool_metric_qa(workspace_id: str, question: str) -> dict:
    """自然语言问数（规则解析指标/时间/意图，无 LLM 也可用）"""
    from ..engine.narrative import ask_metrics
    return await ask_metrics(workspace_id, question)


async def tool_narrate(workspace_id: str, metric: str, days: float = 30,
                       dim: str = "") -> dict:
    """指标自动叙事（结论/异常/趋势/维度贡献/数据质量/建议）"""
    from ..engine.narrative import narrate
    res = await narrate(workspace_id, metric, days=days, dim=dim)
    return {"markdown": res["markdown"], "facts": res["facts"]}


async def tool_data_quality(workspace_id: str, window_days: int = 14) -> dict:
    """数据质量体检（新鲜度/缺口/续采方式）"""
    from ..engine.data_quality import check_workspace
    return await check_workspace(workspace_id, window_days=window_days)


async def tool_attribution(workspace_id: str, days: float = 30,
                           method: str = "linear") -> dict:
    """渠道归因（多触点）"""
    from ..engine.attribution import channel_credit
    return await channel_credit(workspace_id, days=days, method=method)


async def tool_action_lift(workspace_id: str, days: float = 30) -> dict:
    """动作增量（前后对比 + 自助法区间；非随机实验）"""
    from ..engine.attribution import lift_summary
    return await lift_summary(workspace_id, days=days)
