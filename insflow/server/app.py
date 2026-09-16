"""Insight Flow FastAPI 应用"""

from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from ..core.entities import (
    Insight,
    InsightSeverity,
    InsightStatus,
    Workspace,
    WorkspaceStage,
    MaturityLevel,
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
    stage: Optional[str] = "S0"


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
    status: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
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

@app.get("/api/v1/maturity")
async def get_maturity(workspace_id: str = Query(...)):
    """获取成熟度评分"""
    store = await get_store()
    ws = await store.get_workspace(workspace_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return {
        "workspace_id": workspace_id,
        "stage": ws.stage.value,
        "maturity_level": ws.maturity_level.value,
    }
