"""Insight Flow FastAPI 应用"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from ..core.entities import (
    Insight,
    InsightSeverity,
    Workspace,
    WorkspaceStage,
)
from ..core.store import get_store


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期"""
    store = await get_store()
    yield
    await store.close()


app = FastAPI(
    title="Insight Flow",
    description="增长情报与策略操作系统 API",
    version="0.1.0",
    lifespan=lifespan,
)


# ========== 请求/响应模型 ==========

class WorkspaceCreate(BaseModel):
    name: str
    stage: str | None = "S0"


class InsightCreate(BaseModel):
    workspace_id: str
    type: str
    title: str
    summary: str
    severity: str = "medium"
    confidence: float = 0.5
    evidence_json: list[dict] = []
    models_json: list[str] = []
    actions_json: list[dict] = []
    stage_tags_json: list[str] = []


class InsightUpdateStatus(BaseModel):
    status: str


class DiagnosisRequest(BaseModel):
    workspace_id: str
    domain: str
    competitors: list[str] = []


# ========== API 路由 ==========

@app.get("/", response_class=HTMLResponse)
async def index():
    """首页"""
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Insight Flow</title>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 800px; margin: 0 auto; padding: 2rem; }
            h1 { color: #2563eb; }
            .status { background: #f0fdf4; border: 1px solid #86efac; padding: 1rem; border-radius: 0.5rem; }
            .endpoint { background: #f8fafc; padding: 0.5rem 1rem; margin: 0.5rem 0; border-radius: 0.25rem; font-family: monospace; }
        </style>
    </head>
    <body>
        <h1>Insight Flow</h1>
        <p>增长情报与策略操作系统</p>
        <div class="status">
            <strong>Status:</strong> Running ✅<br>
            <strong>Version:</strong> 0.1.0<br>
            <strong>API Docs:</strong> <a href="/docs">/docs</a>
        </div>
        <h2>API Endpoints</h2>
        <div class="endpoint">GET /api/v1/workspaces</div>
        <div class="endpoint">POST /api/v1/workspaces</div>
        <div class="endpoint">GET /api/v1/insights</div>
        <div class="endpoint">POST /api/v1/insights</div>
        <div class="endpoint">POST /api/v1/diagnosis/run</div>
    </body>
    </html>
    """


@app.get("/health")
async def health():
    """健康检查"""
    return {"status": "ok", "version": "0.1.0"}


# ========== 工作区 API ==========

@app.get("/api/v1/workspaces")
async def list_workspaces():
    """列出所有工作区"""
    store = await get_store()
    workspaces = await store.list_workspaces()
    return {"workspaces": [ws.model_dump() for ws in workspaces]}


@app.post("/api/v1/workspaces")
async def create_workspace(data: WorkspaceCreate):
    """创建工作区"""
    store = await get_store()
    ws = Workspace(
        name=data.name,
        stage=WorkspaceStage(data.stage),
    )
    ws = await store.create_workspace(ws)
    return ws.model_dump()


@app.get("/api/v1/workspaces/{workspace_id}")
async def get_workspace(workspace_id: str):
    """获取工作区详情"""
    store = await get_store()
    ws = await store.get_workspace(workspace_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return ws.model_dump()


# ========== 洞察 API ==========

@app.get("/api/v1/insights")
async def list_insights(
    workspace_id: str = Query(...),
    status: str | None = Query(None),
    severity: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
):
    """列出洞察"""
    store = await get_store()
    insights = await store.list_insights(
        workspace_id, status=status, severity=severity, limit=limit
    )
    return {"insights": [ins.model_dump() for ins in insights]}


@app.post("/api/v1/insights")
async def create_insight(data: InsightCreate):
    """创建洞察"""
    store = await get_store()
    insight = Insight(
        workspace_id=data.workspace_id,
        type=data.type,
        title=data.title,
        summary=data.summary,
        severity=InsightSeverity(data.severity),
        confidence=data.confidence,
        evidence_json=data.evidence_json,
        models_json=data.models_json,
        actions_json=data.actions_json,
        stage_tags_json=data.stage_tags_json,
    )
    insight = await store.create_insight(insight)
    return insight.model_dump()


@app.get("/api/v1/insights/{insight_id}")
async def get_insight(insight_id: str):
    """获取洞察详情"""
    store = await get_store()
    insight = await store.get_insight(insight_id)
    if not insight:
        raise HTTPException(status_code=404, detail="Insight not found")
    return insight.model_dump()


@app.post("/api/v1/insights/{insight_id}/ack")
async def acknowledge_insight(insight_id: str):
    """确认洞察"""
    store = await get_store()
    await store.update_insight_status(insight_id, "acknowledged")
    return {"status": "ok"}


@app.post("/api/v1/insights/{insight_id}/dismiss")
async def dismiss_insight(insight_id: str):
    """忽略洞察"""
    store = await get_store()
    await store.update_insight_status(insight_id, "dismissed")
    return {"status": "ok"}


# ========== Agent API ==========

class AgentAskRequest(BaseModel):
    question: str


@app.post("/api/v1/agent/ask")
async def agent_ask(data: AgentAskRequest, workspace_id: str = Query(...)):
    """数据洞察问答（工具调用 + 引用溯源；SSE 流式在 v2 提供）"""
    from ..agent import InsightAgent
    store = await get_store()
    ws = await store.get_workspace(workspace_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found")

    agent = InsightAgent(workspace_id)
    return await agent.ask(data.question)


@app.get("/api/v1/agent/skills")
async def agent_skills():
    """列出已加载的 Agent Skills"""
    from ..agent import get_skills_host
    return {"skills": get_skills_host().list_skills()}


# ========== MCP over HTTP（OpenFlow McpGuard / 任意 MCP HTTP 客户端）==========

@app.get("/api/v1/mcp/tools")
async def mcp_list_tools():
    """MCP 工具清单（10 个，与 stdio server 同源）"""
    from ..mcp_server.tools import MCP_TOOLS_SCHEMA
    return {"tools": MCP_TOOLS_SCHEMA, "transport": "http", "server": "insight-flow"}


class McpToolCall(BaseModel):
    name: str
    arguments: dict = {}


@app.post("/api/v1/mcp/tools/call")
async def mcp_call_tool(data: McpToolCall):
    """MCP 工具调用（HTTP 传输；多 Key 认证由部署层反代注入）"""
    import json as jsonlib

    from ..mcp_server.tools import call_mcp_tool
    output = await call_mcp_tool(data.name, data.arguments)
    try:
        return JSONResponse(content=jsonlib.loads(output))
    except (jsonlib.JSONDecodeError, TypeError):
        return {"raw": output}


# ========== 竞品 / 旅程 / 第一方 API（M3）==========

class CompetitorProfileRequest(BaseModel):
    domain: str
    name: str = ""
    positioning: str = ""
    pricing: list = []
    product_lines: list = []
    social: dict = {}
    monitors: list = []


@app.post("/api/v1/competitors")
async def upsert_competitor(workspace_id: str, data: CompetitorProfileRequest):
    """创建/更新竞品档案（CI-1）"""
    from ..engine.competitor import CompetitorModule
    store = await get_store()
    if not await store.get_workspace(workspace_id):
        raise HTTPException(status_code=404, detail="Workspace not found")
    module = CompetitorModule(workspace_id)
    return await module.upsert_profile(
        data.domain, name=data.name, positioning=data.positioning,
        pricing=data.pricing, product_lines=data.product_lines,
        social=data.social, monitors=data.monitors,
    )


@app.get("/api/v1/competitors")
async def list_competitors(workspace_id: str = Query(...)):
    """列出竞品档案"""
    from ..engine.competitor import CompetitorModule
    return {"competitors": await CompetitorModule(workspace_id).list_profiles()}


class SeoSnapshotRequest(BaseModel):
    domain: str
    ranks: list[dict]


@app.post("/api/v1/competitors/seo-snapshot")
async def seo_snapshot(workspace_id: str, data: SeoSnapshotRequest):
    """记录关键词排名快照 → 关键词缺口分析（CI-3）"""
    from ..engine.competitor import CompetitorModule
    module = CompetitorModule(workspace_id)
    return await module.record_seo_snapshot(data.domain, data.ranks)


class FirstPartyAnalysisRequest(BaseModel):
    members: list[dict] = []
    orders: list[dict] = []


@app.post("/api/v1/reports/weekly")
async def build_weekly(workspace_id: str = Query(...)):
    """生成增长周报（无人值守）+ 同步落 MFlow 报告目录"""
    from ..engine.weekly_report import WeeklyReportBuilder
    store = await get_store()
    if not await store.get_workspace(workspace_id):
        raise HTTPException(status_code=404, detail="Workspace not found")
    builder = WeeklyReportBuilder(workspace_id)
    return await builder.build()


@app.post("/api/v1/first-party/analyze")
async def first_party_analyze(workspace_id: str, data: FirstPartyAnalysisRequest):
    """第一方情报：旅程重建（CJ-3）+ RFM 分层（CJ-5）

    members/orders 留空时走 OpenFlow MCP 客户端实时拉取。
    """
    from ..engine.first_party import FirstPartyIntelligence
    fi = FirstPartyIntelligence(workspace_id)
    return await fi.run_first_party_analysis(
        members=data.members or None, orders=data.orders or None,
    )


# ========== 入站事件（HMAC 验签）==========

@app.post("/api/v1/ingest")
async def ingest(request: Request):
    """入站事件接收（MFlow 发布回流 / OpenFlow 钩子，HMAC 验签）

    - MFlow: content.published → 关联洞察动作 → 验证状态机
    - OpenFlow: cdp_event/lead/contact → 事件流记录
    """
    from ..actions.ingest import IngestReceiver, extract_signature

    raw_body = await request.body()
    signature = extract_signature(request.headers)
    receiver = IngestReceiver()
    return await receiver.handle(raw_body, request.headers, signature)


@app.get("/api/v1/feedback/stats")
async def feedback_stats(workspace_id: str = Query(...)):
    """模型效果统计（北极星指标）"""
    store = await get_store()
    ws = await store.get_workspace(workspace_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return await store.get_feedback_stats(workspace_id)


# ========== 动作 API ==========

class ActionDispatchRequest(BaseModel):
    workspace_id: str
    insight_id: str
    action_type: str
    target_ref: str | None = None
    params_json: dict = {}
    description: str = ""
    title: str = ""
    summary: str = ""
    severity: str = "medium"
    confidence: float = 0.5


@app.post("/api/v1/actions")
async def dispatch_action(data: ActionDispatchRequest):
    """派发推荐动作（AC-3：Action Router → 目标系统）"""
    from ..actions.router import ActionContext, get_action_router

    store = await get_store()
    ws = await store.get_workspace(data.workspace_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found")

    router = get_action_router()
    result = await router.dispatch(
        {
            "action_type": data.action_type,
            "target_ref": data.target_ref,
            "params_json": data.params_json,
            "description": data.description,
            "title": data.title,
            "summary": data.summary,
            "severity": data.severity,
            "confidence": data.confidence,
        },
        ActionContext(workspace_id=data.workspace_id, insight_id=data.insight_id),
    )
    return result


@app.get("/api/v1/actions/types")
async def list_action_types():
    """列出已注册的动作适配器类型"""
    from ..actions.router import get_action_router
    return {"action_types": get_action_router().list_types()}


# ========== 诊断 API ==========

@app.post("/api/v1/diagnosis/run")
async def run_diagnosis(data: DiagnosisRequest):
    """触发诊断（同步执行，产出洞察 + 报告落盘）"""
    from ..engine.diagnosis import DiagnosisEngine

    ws = await get_store().get_workspace(data.workspace_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found")

    engine = DiagnosisEngine(data.workspace_id)
    result = await engine.run()
    return result


# ========== 成熟度 API ==========

class MaturityAssessRequest(BaseModel):
    answers: dict[str, int]
    monthly_sessions: float = 0
    conversion_rate: float = 0.0
    user_confirmed_stage: str | None = None
    months_since_launch: int | None = None


@app.post("/api/v1/maturity/assess")
async def assess_maturity(workspace_id: str, data: MaturityAssessRequest):
    """执行成熟度评估（DM-1 问卷评分 + DM-4 阶段判定 + 报告落盘）"""
    from ..engine.maturity import MaturityEngine

    store = await get_store()
    ws = await store.get_workspace(workspace_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found")

    engine = MaturityEngine(workspace_id)
    result = await engine.assess(
        data.answers,
        monthly_sessions=data.monthly_sessions,
        conversion_rate=data.conversion_rate,
        user_confirmed_stage=data.user_confirmed_stage,
        months_since_launch=data.months_since_launch,
    )
    return result


@app.get("/api/v1/maturity/questionnaire")
async def get_questionnaire():
    """获取成熟度评估问卷定义"""
    from ..engine.maturity import QUESTIONNAIRE
    return {"dimensions": QUESTIONNAIRE}


@app.get("/api/v1/maturity")
async def get_maturity(workspace_id: str = Query(...)):
    """获取成熟度评分与阶段"""
    store = await get_store()
    ws = await store.get_workspace(workspace_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return {
        "workspace_id": workspace_id,
        "stage": ws.stage.value,
        "maturity_level": ws.maturity_level.value,
    }
