"""Insight Flow Agent 工具集

Agent 消费的工具（默认只读；写操作全部走"可控写入"链路）：
- query_insights：按条件检索洞察流
- get_insight_detail：洞察详情（含 evidence 供引用溯源）
- list_models：列出可运行的洞察模型
- get_feedback_stats：动作验证效果统计（北极星）
- list_reports：报告中心目录
- propose_action：**起草待审批动作**（进 pending，人工批准才派发）
- save_note / recall_notes：工作区记忆（带引用与版本，可检索）
- schedule_task：把这套分析固化为定时任务（每日/每周跑并推送）
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
    {
        "type": "function",
        "function": {
            "name": "propose_action",
            "description": ("起草一个**待人工审批**的动作（不会立即执行）。"
                            "适用于用户要求'去执行/派发/通知/建内容'且已有对应洞察时；"
                            "必须引用洞察 ID，并说明理由。人批准后进入 14 天验证。"),
            "parameters": {"type": "object", "properties": {
                "workspace_id": {"type": "string"},
                "insight_id": {"type": "string", "description": "依据的洞察 ID（必填，保证引用溯源）"},
                "action_type": {"type": "string",
                                "description": "动作类型，如 feishu.notify / webhook.generic / mflow.create_content / openflow.webhook_insight"},
                "target_ref": {"type": "string", "description": "目标地址（未配置时留空用工作区默认）"},
                "params": {"type": "object", "description": "动作参数（可选）"},
                "rationale": {"type": "string", "description": "为什么建议这个动作（一到两句）"},
            }, "required": ["workspace_id", "insight_id", "action_type"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_note",
            "description": ("把值得记住的结论/偏好/背景存入工作区记忆（之后问答会自动带上）。"
                            "同标题会更新并把版本 +1。"),
            "parameters": {"type": "object", "properties": {
                "workspace_id": {"type": "string"},
                "title": {"type": "string"},
                "body": {"type": "string", "description": "记忆正文（含关键数字）"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "citations": {"type": "array", "items": {"type": "string"},
                              "description": "相关洞察 ID 列表"},
            }, "required": ["workspace_id", "title", "body"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_notes",
            "description": "检索工作区记忆（此前沉淀的结论/偏好/背景）。",
            "parameters": {"type": "object", "properties": {
                "workspace_id": {"type": "string"}, "query": {"type": "string"},
                "limit": {"type": "integer", "default": 10}},
                "required": ["workspace_id"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_task",
            "description": ("把这个问题存为**定时任务**（每日/每周自动跑一次并把答案推送到飞书/Webhook）。"
                            "用户说'以后每天/每周帮我盯一下…'时使用。"),
            "parameters": {"type": "object", "properties": {
                "workspace_id": {"type": "string"},
                "question": {"type": "string", "description": "要定期执行的问题（可含指标/时间范围）"},
                "cron": {"type": "string", "enum": ["hourly", "daily", "weekly"], "default": "daily"},
                "name": {"type": "string", "description": "任务名（默认取问题前 40 字）"},
            }, "required": ["workspace_id", "question"]},
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
    "propose_action": lambda args: tool_propose_action(**args),
    "save_note": lambda args: tool_save_note(**args),
    "recall_notes": lambda args: tool_recall_notes(**args),
    "schedule_task": lambda args: tool_schedule_task(**args),
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

# ========== 可控写入：动作提案 / 工作区记忆 / 定时任务（批次 C） ==========

async def tool_propose_action(workspace_id: str, insight_id: str, action_type: str,
                              target_ref: str = "", params: dict | None = None,
                              rationale: str = "") -> dict:
    """起草待审批动作（Agent 不能直接派发；人批准后才进 14 天验证）"""
    from ..engine.proposals import propose_action
    return await propose_action(
        workspace_id, insight_id=insight_id, action_type=action_type,
        target_ref=target_ref, params=params or {}, rationale=rationale,
        proposed_by="agent")


async def tool_save_note(workspace_id: str, title: str, body: str,
                         tags: list | None = None,
                         citations: list | None = None) -> dict:
    """写入工作区记忆（同标题更新并 version+1）"""
    from ..engine.agent_memory import save_note
    note = await save_note(workspace_id, title=title, body=body, tags=tags,
                           citations=citations)
    return {"ok": True, "note_id": note.get("id"), "note_key": note.get("note_key"),
            "version": note.get("version"), "updated_at": note.get("updated_at")}


async def tool_recall_notes(workspace_id: str, query: str = "",
                            limit: int = 10) -> dict:
    """检索工作区记忆"""
    from ..engine.agent_memory import recall_notes
    notes = await recall_notes(workspace_id, query, limit=limit)
    return {"total": len(notes), "notes": [{
        "id": n.get("id"), "title": n.get("title"), "body": (n.get("body") or "")[:500],
        "tags": n.get("tags_json"), "citations": n.get("citations_json"),
        "version": n.get("version"), "updated_at": n.get("updated_at"),
    } for n in notes]}


async def tool_schedule_task(workspace_id: str, question: str, cron: str = "daily",
                             name: str = "") -> dict:
    """把问题固化为定时任务（hourly/daily/weekly，跑完推送飞书/Webhook）"""
    from ..engine.agent_tasks import create_task
    task = await create_task(workspace_id, name=name or question[:40],
                             question=question, cron=cron, created_by="agent")
    return {"ok": True, "task_id": task.get("id"), "cron": task.get("cron"),
            "scheduled": task.get("scheduled"),
            "message": "已创建定时任务；每次执行后按通道推送（可在控制台 Agent 页管理）"}
