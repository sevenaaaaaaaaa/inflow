"""Insight Flow FastAPI 应用"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

# 环境配置（支持仓库根目录 .env：INSFLOW_SAAS / INSFLOW_BASE_PATH / SMTP_* 等）
try:
    from pathlib import Path as _Path
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(_Path(__file__).parent.parent.parent / ".env")
except Exception:
    pass

from .. import __version__
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
    # 定时任务集成（监控恢复/验证评估/周报/配额审计，可经 INSFLOW_DISABLE_SCHEDULER=1 关闭）
    if _os.environ.get("INSFLOW_DISABLE_SCHEDULER", "") != "1":
        try:
            from ..core.bootstrap import bootstrap_scheduled_jobs
            await bootstrap_scheduled_jobs()
        except Exception:
            import logging
            logging.getLogger("insflow").exception("定时任务引导失败（服务继续可用）")
    yield
    from ..core.scheduler import get_scheduler
    get_scheduler().shutdown()
    await store.close()


app = FastAPI(
    title="Insight Flow",
    description="增长情报与策略操作系统 API",
    version=__version__,
    lifespan=lifespan,
    # OpenAPI 3.1（可直接导入 n8n / Dify / Postman 生成节点与 SDK）
    openapi_version="3.1.0",
    openapi_url="/api/v1/openapi.json",
    docs_url="/api/v1/docs",
    redoc_url="/api/v1/redoc",
)

# ========== API Key 认证（可选启用，M4）==========

import os as _os
import time

from ..core.auth import AuthManager

_auth_managers: dict[str, AuthManager] = {}


def _get_auth(workspace_id: str) -> AuthManager:
    if workspace_id not in _auth_managers:
        _auth_managers[workspace_id] = AuthManager(workspace_id)
    return _auth_managers[workspace_id]


AUTH_ENABLED = _os.environ.get("INSFLOW_API_AUTH", "") == "1"

# Web 控制台（Jinja2 SSR，零构建链）
from ..web.routes import router as console_router  # noqa: E402
app.include_router(console_router)

# ========== SaaS 多租户守卫（R4-1：INSFLOW_SAAS=1 时控制台要求登录）==========

BASE_PATH = _os.environ.get("INSFLOW_BASE_PATH", "").rstrip("/")


@app.middleware("http")
async def no_store_middleware(request, call_next):
    """控制台/REST 响应禁止中间缓存（防跨会话命中，宝塔/Apache 默认会缓存 HTML）"""
    response = await call_next(request)
    path = request.url.path
    if path.startswith(("/console", "/api/")):
        response.headers["Cache-Control"] = "private, no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


@app.middleware("http")
async def saas_guard(request, call_next):
    if _os.environ.get("INSFLOW_SAAS", "") != "1":
        return await call_next(request)
    path = request.url.path
    if path.startswith("/console") and not path.startswith(
            ("/console/login", "/console/register", "/console/logout",
             "/console/sso/")):
        from urllib.parse import quote
        from fastapi.responses import RedirectResponse
        from ..core.accounts import COOKIE_NAME, AccountManager
        user = await AccountManager().verify_session(request.cookies.get(COOKIE_NAME))
        # /console/embed 由 HMAC 令牌鉴权（第三方 iframe 无会话 Cookie）：
        # 不重定向（有会话则照常带上身份）；/console/embed/token 仍需登录。
        exempt = path in ("/console/embed", "/console/manifest.webmanifest",
                          "/console/sw.js", "/console/icon.svg")
        if user:
            request.state.user = user
        elif not exempt:
            return RedirectResponse(
                f"{BASE_PATH}/console/login?next={quote(path)}", status_code=303)
    return await call_next(request)


# ========== 请求耗时观测（后台性能，对齐 OpenFlow p95 指标）==========

PERF_SAMPLES = 300
_perf = {"count": 0, "slow": 0, "durations": [], "threshold_ms": 800.0,
         "slow_paths": {}}


@app.middleware("http")
async def perf_middleware(request, call_next):
    import time as _t
    t0 = _t.perf_counter()
    response = await call_next(request)
    elapsed = (_t.perf_counter() - t0) * 1000
    _perf["count"] += 1
    ds = _perf["durations"]
    ds.append(elapsed)
    if len(ds) > PERF_SAMPLES:
        del ds[0]
    if elapsed >= _perf["threshold_ms"]:
        _perf["slow"] += 1
        path = request.url.path
        _perf["slow_paths"][path] = _perf["slow_paths"].get(path, 0) + 1
        import logging
        logging.getLogger("insflow.web").warning("slow page %.0fms %s", elapsed, path)
    response.headers["X-Response-Time"] = f"{elapsed:.0f}ms"
    return response


def perf_stats() -> dict:
    ds = sorted(_perf["durations"])
    return {
        "requests": _perf["count"],
        "slow_count": _perf["slow"],
        "threshold_ms": int(_perf["threshold_ms"]),
        "p50_ms": round(ds[len(ds) // 2], 1) if ds else 0.0,
        "p95_ms": round(ds[int(len(ds) * 0.95)], 1) if ds else 0.0,
        "max_ms": round(max(ds), 1) if ds else 0.0,
        "slow_paths": sorted(_perf["slow_paths"].items(), key=lambda kv: -kv[1])[:5],
    }


# ========== 基路径重写（子路径反代，如 https://host/inflow）==========
# 注意：本中间件必须最后注册（最外层），才能改写守卫返回的 Location 头。

def _rewrite_html(text: str) -> str:
    if not BASE_PATH:
        return text
    for q in ('"', "'"):
        text = text.replace(f"href={q}/", f"href={q}{BASE_PATH}/")
        text = text.replace(f"action={q}/", f"action={q}{BASE_PATH}/")
        text = text.replace(f"{q}/api/v1/", f"{q}{BASE_PATH}/api/v1/")
        text = text.replace(f"{q}/console", f"{q}{BASE_PATH}/console")
    return text


@app.middleware("http")
async def base_path_middleware(request, call_next):
    response = await call_next(request)
    if not BASE_PATH:
        return response
    loc = response.headers.get("location")
    if loc and loc.startswith("/") and not loc.startswith(BASE_PATH + "/"):
        response.headers["location"] = BASE_PATH + loc
    ctype = response.headers.get("content-type", "")
    if ctype.startswith("text/html") and hasattr(response, "body_iterator"):
        from starlette.responses import Response
        body = b"".join([chunk async for chunk in response.body_iterator])
        new = Response(content=_rewrite_html(body.decode("utf-8", "ignore")).encode("utf-8"),
                       status_code=response.status_code, media_type="text/html")
        for k, v in response.raw_headers:
            if k.lower() in (b"content-length", b"content-type"):
                continue
            new.raw_headers.append((k, v))
        return new
    return response


async def require_auth(request, action: str):
    """可选鉴权依赖：INSFLOW_API_AUTH=1 时校验 X-API-Key + RBAC/scope"""
    if not AUTH_ENABLED:
        return None
    key = request.headers.get("X-API-Key", "")
    workspace_id = request.query_params.get("workspace_id", "default")
    ok, api_key = _get_auth(workspace_id).authorize(key, action)
    if not ok:
        raise HTTPException(status_code=401, detail="认证/授权失败")
    return api_key


class APIKeyCreate(BaseModel):
    name: str
    scopes: list[str] = ["read"]
    ttl_days: int | None = 365


@app.post("/api/v1/auth/keys")
async def create_api_key(workspace_id: str, data: APIKeyCreate):
    """创建 API Key（明文只返回一次）"""
    from ..core.auth import SCOPE_LEVEL
    for s in data.scopes:
        if s not in SCOPE_LEVEL:
            raise HTTPException(status_code=422, detail=f"非法 scope: {s}")
    key = _get_auth(workspace_id).create_key(data.name, data.scopes)
    return {"key_id": key.key_id, "key": key.key, "name": key.name,
            "scopes": key.scopes, "expires_at": key.expires_at.isoformat() if key.expires_at else None}


@app.get("/api/v1/auth/keys")
async def list_api_keys(workspace_id: str = Query(...)):
    return {"keys": _get_auth(workspace_id).list_keys()}


@app.delete("/api/v1/auth/keys/{key_id}")
async def revoke_api_key(workspace_id: str, key_id: str):
    ok = _get_auth(workspace_id).revoke(key_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Key not found")
    return {"ok": True}


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
            <strong>Version:</strong> {__version__}<br>
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
    """健康检查（no-store：版本号实时可见，不被 CDN 缓存误导）"""
    return JSONResponse(
        content={"status": "ok", "version": __version__},
        headers={"Cache-Control": "no-store"},
    )


# ========== 工作区 API ==========

@app.get("/api/v1/workspaces")
async def list_workspaces():
    """列出所有工作区"""
    store = await get_store()
    workspaces = await store.list_workspaces()
    return {"workspaces": [ws.model_dump() for ws in workspaces]}


# ========== 接入向导（TD-1，M7）==========

class OAuthStartRequest(BaseModel):
    provider: str  # gsc | ga4


class OAuthExchangeRequest(BaseModel):
    provider: str
    code: str
    state: str


class APICredentialRequest(BaseModel):
    provider: str
    credential: str
    site: str = ""


@app.post("/api/v1/onboarding/oauth/start")
async def oauth_start(workspace_id: str, data: OAuthStartRequest):
    """生成 OAuth 授权跳转 URL（须先配置 GOOGLE_OAUTH_CLIENT_ID）"""
    from ..engine.onboarding import OnboardingService
    client_id = _os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
    if not client_id:
        raise HTTPException(status_code=422, detail="GOOGLE_OAUTH_CLIENT_ID 未配置")
    base = _os.environ.get("INSFLOW_BASE_URL", "http://localhost:8400").rstrip("/")
    svc = OnboardingService(workspace_id)
    return {
        "auth_url": svc.auth_url(
            data.provider, client_id,
            redirect_uri=f"{base}/api/v1/onboarding/oauth/callback",
        ),
    }


@app.post("/api/v1/onboarding/oauth/callback")
async def oauth_callback(workspace_id: str, data: OAuthExchangeRequest):
    """OAuth 回调交换（code → token → 保险库；state 一次性防重放）"""
    from ..engine.onboarding import OnboardingService
    client_id = _os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
    client_secret = _os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise HTTPException(status_code=422, detail="GOOGLE_OAUTH_CLIENT_ID/SECRET 未配置")
    base = _os.environ.get("INSFLOW_BASE_URL", "http://localhost:8400").rstrip("/")
    svc = OnboardingService(workspace_id)
    result = await svc.exchange_code(
        data.provider, data.code, data.state, client_id, client_secret,
        f"{base}/api/v1/onboarding/oauth/callback",
    )
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/api/v1/onboarding/credential")
async def save_credential(workspace_id: str, data: APICredentialRequest):
    """API Key 类凭据直配（CrUX / 手动粘贴 token 等）→ 保险库"""
    from ..engine.onboarding import OnboardingService
    svc = OnboardingService(workspace_id)
    result = await svc.save_api_credential(data.provider, data.credential, data.site)
    if not result["ok"]:
        raise HTTPException(status_code=422, detail=result["error"])
    return result


@app.get("/api/v1/onboarding/health")
async def onboarding_health(workspace_id: str, provider: str = Query(...)):
    """接入健康检查（DM-2 实测能否拉数）"""
    from ..engine.onboarding import OnboardingService
    return await OnboardingService(workspace_id).check_health(provider)


# ========== 监控任务（M6）==========

class MonitorCreateRequest(BaseModel):
    kind: str
    target: dict
    schedule_cron: str = "0 */6 * * *"


@app.get("/api/v1/monitors")
async def list_monitors(workspace_id: str = Query(...)):
    from ..engine.monitors import MonitorService
    return {"monitors": await MonitorService(workspace_id).list()}


@app.post("/api/v1/monitors")
async def create_monitor(workspace_id: str, data: MonitorCreateRequest):
    from ..engine.monitors import MonitorService
    try:
        return await MonitorService(workspace_id).create(data.kind, data.target, data.schedule_cron)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.post("/api/v1/monitors/{monitor_id}/run")
async def run_monitor(workspace_id: str, monitor_id: str):
    from ..engine.monitors import MonitorService
    result = await MonitorService(workspace_id).run(monitor_id)
    if "error" in result and "not found" in result["error"]:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.delete("/api/v1/monitors/{monitor_id}")
async def delete_monitor(workspace_id: str, monitor_id: str):
    from ..engine.monitors import MonitorService
    ok = await MonitorService(workspace_id).delete(monitor_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Monitor not found")
    return {"ok": True}


# ========== 计费 / 白标 / 多租户（M5）==========

class PlanSetRequest(BaseModel):
    plan_id: str


@app.get("/api/v1/billing/usage")
async def billing_usage(workspace_id: str = Query(...)):
    """用量可见（套餐 + 配额百分比，前端用量页直读）"""
    from ..engine.billing import BillingManager
    store = await get_store()
    if not await store.get_workspace(workspace_id):
        raise HTTPException(status_code=404, detail="Workspace not found")
    mgr = BillingManager(workspace_id)
    return await mgr.usage_summary()


@app.post("/api/v1/billing/plan")
async def set_plan(workspace_id: str, data: PlanSetRequest):
    """切换套餐（云托管计费系统调用）"""
    from ..engine.billing import BillingManager
    mgr = BillingManager(workspace_id)
    try:
        return await mgr.set_plan(data.plan_id)
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.get("/api/v1/billing/trial")
async def trial_status(workspace_id: str = Query(...)):
    """试用状态（G-4 自助试用）"""
    from ..engine.billing import BillingManager
    return await BillingManager(workspace_id).trial_status()


@app.post("/api/v1/billing/trial")
async def start_trial(workspace_id: str = Query(...), days: int = Query(14)):
    """开通试用（注册即自动调用；此处供补开/运维）"""
    from ..engine.billing import BillingManager
    try:
        return await BillingManager(workspace_id).start_trial(days=days)
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.post("/api/v1/billing/invoice")
async def build_invoice(workspace_id: str = Query(...), month: str | None = Query(None)):
    """生成月度对账单（R3-3，客户可读 Markdown）"""
    from ..engine.invoice import InvoiceBuilder
    store = await get_store()
    if not await store.get_workspace(workspace_id):
        raise HTTPException(status_code=404, detail="Workspace not found")
    result = await InvoiceBuilder(workspace_id).build(month=month)
    return {"invoice_path": result["invoice_path"]}


# ========== 行业模板包（R3-1）==========

class TemplateApplyRequest(BaseModel):
    template_id: str


@app.get("/api/v1/metrics/catalog")
async def metrics_catalog(workspace_id: str = Query(...), days: float = Query(90)):
    """可用指标目录（即席探索）"""
    store = await get_store()
    return {"metrics": await store.metric_catalog(workspace_id, days)}


@app.get("/api/v1/ui/layout")
async def api_load_layout(workspace_id: str = Query(""), path: str = Query("")):
    """读取看板布局（跨设备；无则返回空数组，前端回落 localStorage）"""
    store = await get_store()
    ws = await store.get_workspace(workspace_id)
    layouts = dict((ws.settings_json or {}).get("layouts") or {}) if ws else {}
    return {"ok": True, "path": path, "order": layouts.get(path, [])}


@app.put("/api/v1/ui/layout")
async def api_save_layout(payload: dict):
    """保存看板布局（按路径存于工作区设置，不需要新表/迁移）"""
    workspace_id = str(payload.get("workspace_id") or "")
    path = str(payload.get("path") or "")[:200]
    order = [str(x)[:60] for x in (payload.get("order") or [])][:80]
    if not workspace_id or not path:
        raise HTTPException(status_code=400, detail="需要 workspace_id 与 path")
    store = await get_store()
    ws = await store.get_workspace(workspace_id)
    if not ws:
        raise HTTPException(status_code=404, detail="工作区不存在")
    settings = dict(ws.settings_json or {})
    layouts = dict(settings.get("layouts") or {})
    prev = layouts.get(path)
    layouts[path] = order
    settings["layouts"] = layouts
    if prev and prev != order:      # 版本历史（可回溯，最多留 20 版）
        history = list(settings.get("layouts_history") or [])
        history.append({"path": path, "order": prev,
                        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
        settings["layouts_history"] = history[-20:]
    ws.settings_json = settings
    await store.update_workspace(ws)
    return {"ok": True, "path": path, "order": order}


# ========== 即席探索 / 语义层 / 协作 / 告警 / 估算（P1-P2 能力面） ==========

def _role_of(request) -> str:
    """当前请求角色（SaaS 会话；无会话视为 owner，兼容私有化单租户）"""
    user = getattr(request.state, "user", None)
    if isinstance(user, dict):
        return str(user.get("role") or "owner")
    return "owner"


def _guard(request, action: str):
    from ..engine.permissions import PermissionError_, require
    try:
        require(_role_of(request), action)
    except PermissionError_ as e:
        raise HTTPException(status_code=403, detail=str(e))


@app.post("/api/v1/explore/pivot")
async def explore_pivot(request: Request, workspace_id: str = Query(...),
                        metric: str = Query(...), dim: str = Query(...),
                        dim2: str = Query(""), days: float = Query(30)):
    """二维透视（即席探索）：维度 × 维度 / 维度 × 时间"""
    _guard(request, "read")
    store = await get_store()
    return await store.pivot(workspace_id, metric, dim, dim2, days=days)


# ========== 实时流（SSE）与在线协同（presence） ==========

@app.get("/api/v1/stream")
async def stream(request: Request, workspace_id: str = Query(...),
                 metrics: str = Query(""), days: float = Query(7),
                 interval: float = Query(5), max_ticks: int = Query(600),
                 path: str = Query("")):
    """SSE 实时流：metrics（指标快照）/ heartbeat / alert / done

    - 客户端断开即结束（避免连接泄漏与测试/资源占用）
    - 反代注意：需关闭响应缓冲（Nginx `proxy_buffering off`）；已带 X-Accel-Buffering: no
    """
    from fastapi.responses import StreamingResponse
    from ..engine.realtime import StreamHub, parse_metrics, sse_event
    names = parse_metrics(metrics) or ["ga4_sessions"]

    async def _gen():
        hub = StreamHub(workspace_id, metrics=names, days=days,
                        interval=max(2.0, min(60.0, interval)),
                        max_ticks=max(1, min(2000, max_ticks)))
        async for event, data in hub.events():
            if await request.is_disconnected():
                return
            yield sse_event(event, data)
    return StreamingResponse(_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store",
                                      "X-Accel-Buffering": "no",
                                      "Connection": "keep-alive"})


@app.post("/api/v1/presence")
async def presence_update(request: Request, payload: dict):
    """上报在线状态/光标（前端节流调用，约 10s 一次）"""
    from ..engine.realtime import presence
    ws = str(payload.get("workspace_id") or "")
    conn_id = str(payload.get("conn_id") or "")
    if not ws or not conn_id:
        raise HTTPException(status_code=400, detail="需要 workspace_id 与 conn_id")
    user = getattr(request.state, "user", None) or {}
    presence.touch(ws, conn_id[:64], user=str(user.get("email") or
                                            payload.get("user") or "本地用户"),
                   path=str(payload.get("path") or "")[:200],
                   cursor=payload.get("cursor") if isinstance(payload.get("cursor"), dict)
                   else None)
    return {"ok": True, "online": len(presence.snapshot(ws))}


@app.get("/api/v1/presence")
async def presence_list(workspace_id: str = Query(...)):
    """当前在线成员与光标（谁在和我看同一个看板）"""
    from ..engine.realtime import presence
    return {"online": presence.snapshot(workspace_id)}


@app.post("/api/v1/presence/leave")
async def presence_leave(payload: dict):
    from ..engine.realtime import presence
    presence.leave(str(payload.get("workspace_id") or ""),
                   str(payload.get("conn_id") or ""))
    return {"ok": True}


@app.post("/api/v1/rls/policies")
async def set_rls_policies(request: Request, payload: dict):
    """设置表达式级 RLS 策略（owner/admin）：{"viewer": ["channel = 'search'"]}

    写入前逐条试编译，语法错误直接 400（避免把坏策略存进去导致全员被拒）。
    """
    _guard(request, "workspace.write" if _role_of(request) in ("owner", "admin")
           else "alert.write")
    from ..engine.rls import PolicyError, compile_policy
    from ..engine.permissions import ROLES
    ws = str(payload.get("workspace_id") or "")
    policies = payload.get("policies") or {}
    if not ws or not isinstance(policies, dict):
        raise HTTPException(status_code=400, detail="需要 workspace_id 与 policies")
    clean: dict[str, list[str]] = {}
    for role, exprs in policies.items():
        if role not in ROLES:
            raise HTTPException(status_code=400, detail=f"未知角色：{role}")
        items = [exprs] if isinstance(exprs, str) else list(exprs or [])
        compiled = []
        for expr in items[:5]:
            try:
                compile_policy(str(expr), {"email": "probe@test", "role": role})
            except PolicyError as e:
                raise HTTPException(status_code=400,
                                    detail=f"策略语法错误（{role}）：{e}")
            compiled.append(str(expr))
        clean[role] = compiled
    store = await get_store()
    ws_row = await store.get_workspace(ws)
    if not ws_row:
        raise HTTPException(status_code=404, detail="工作区不存在")
    settings = dict(ws_row.settings_json or {})
    settings["rls_policies"] = clean
    ws_row.settings_json = settings
    await store.update_workspace(ws_row)
    return {"ok": True, "policies": clean}


@app.get("/api/v1/rls/policies")
async def get_rls_policies(workspace_id: str = Query(...)):
    """当前 RLS 策略（含角色）"""
    ws = await (await get_store()).get_workspace(workspace_id)
    return {"policies": dict(((ws.settings_json or {}).get("rls_policies") or {}))
            if ws else {}}


@app.get("/api/v1/dims")
async def list_dims(workspace_id: str = Query(...), metric: str = Query("")):
    """可用维度清单（拖拽式探索的"字段面板"）"""
    return {"dims": await (await get_store()).dim_keys(workspace_id, metric)}


@app.post("/api/v1/explore/cube")
async def explore_cube(request: Request, payload: dict):
    """自由组合透视：rows(1-2 维) × cols(0-1 维)，拖拽式探索后端"""
    _guard(request, "read")
    store = await get_store()
    ws = str(payload.get("workspace_id") or "")
    metric = str(payload.get("metric") or "")
    if not ws or not metric:
        raise HTTPException(status_code=400, detail="需要 workspace_id 与 metric")
    return await store.cube(ws, metric, list(payload.get("rows") or []),
                            str(payload.get("cols") or ""),
                            days=float(payload.get("days") or 30),
                            agg=str(payload.get("agg") or "sum"))


@app.post("/api/v1/explore/sql")
async def explore_sql_api(request: Request, payload: dict):
    """只读 SQL 沙箱（白名单表 + 强制租户隔离 + LIMIT）"""
    _guard(request, "explore.sql")
    from ..engine.explore_sql import SqlError, run as sql_run
    try:
        return await sql_run(str(payload.get("workspace_id") or ""),
                             str(payload.get("sql") or ""))
    except SqlError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/v1/metrics/defs")
async def list_metric_defs(workspace_id: str = Query(...)):
    """语义层：指标口径清单（含版本/责任人）"""
    return {"defs": await (await get_store()).list_metric_defs(workspace_id)}


@app.post("/api/v1/metrics/defs")
async def upsert_metric_def(request: Request, payload: dict):
    """登记/更新指标口径（expr 变更自动升版本）"""
    _guard(request, "workspace_id.write" if False else "insight.write")
    store = await get_store()
    ws = str(payload.get("workspace_id") or "")
    name = str(payload.get("name") or "").strip()
    if not ws or not name:
        raise HTTPException(status_code=400, detail="需要 workspace_id 与 name")
    return await store.upsert_metric_def(ws, name, **{
        k: payload.get(k) for k in ("label", "expr", "unit", "owner", "notes")
        if payload.get(k) is not None})


@app.get("/api/v1/comments")
async def list_comments(workspace_id: str = Query(...), target_type: str = Query(""),
                        target_id: str = Query("")):
    """协作评论列表"""
    return {"comments": await (await get_store()).list_comments(
        workspace_id, target_type, target_id)}


@app.post("/api/v1/comments")
async def add_comment(request: Request, payload: dict):
    """新增评论（协作）"""
    _guard(request, "read")
    store = await get_store()
    ws = str(payload.get("workspace_id") or "")
    body = str(payload.get("body") or "").strip()
    if not ws or not body:
        raise HTTPException(status_code=400, detail="需要 workspace_id 与 body")
    user = getattr(request.state, "user", None) or {}
    author = str(payload.get("author") or user.get("email") or "本地用户")
    return await store.add_comment(ws, str(payload.get("target_type") or "insight"),
                                   str(payload.get("target_id") or ""), body, author)


@app.get("/api/v1/alerts/rules")
async def list_alert_rules(workspace_id: str = Query(...)):
    """阈值告警规则列表"""
    return {"rules": await (await get_store()).list_alert_rules(workspace_id)}


@app.post("/api/v1/alerts/rules")
async def upsert_alert_rule(request: Request, payload: dict):
    """新建/更新阈值告警规则（含路由与升级策略）"""
    _guard(request, "alert.write")
    ws = str(payload.get("workspace_id") or "")
    if not ws:
        raise HTTPException(status_code=400, detail="需要 workspace_id")
    return await (await get_store()).upsert_alert_rule(ws, payload)


@app.delete("/api/v1/alerts/rules/{rule_id}")
async def delete_alert_rule(request: Request, workspace_id: str = Query(...),
                            rule_id: str = ""):
    _guard(request, "alert.write")
    ok = await (await get_store()).delete_alert_rule(workspace_id, rule_id)
    return {"ok": ok}


@app.post("/api/v1/alerts/check")
async def alerts_check(request: Request, workspace_id: str = Query(...)):
    """立即评估阈值规则（命中生成洞察 + 按路由通知）并检查升级"""
    _guard(request, "alert.write")
    from ..engine.alerts import evaluate_workspace, sweep_escalations
    return {"fired": await evaluate_workspace(workspace_id),
            "escalations": await sweep_escalations(workspace_id)}


@app.post("/api/v1/subscriptions/metric")
async def create_metric_subscription(request: Request, payload: dict):
    """图表级订阅：按期推送某指标的图（含同环比/异常/预测）"""
    _guard(request, "subscription.write")
    from ..engine.subscriptions import SubscriptionError, SubscriptionService
    ws = str(payload.get("workspace_id") or "")
    if not ws:
        raise HTTPException(status_code=400, detail="需要 workspace_id")
    try:
        return await SubscriptionService(ws).create_metric(
            str(payload.get("name") or "指标图订阅"),
            str(payload.get("metric") or ""),
            list(payload.get("channels") or ["webhook"]),
            dict(payload.get("target") or {}),
            chart=str(payload.get("chart") or "line"),
            days=float(payload.get("days") or 30),
            compare_prev=bool(payload.get("compare_prev", True)))
    except SubscriptionError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/v1/vars")
async def template_vars(workspace_id: str = Query(...), metric: str = Query(""),
                        dims: str = Query("province,channel,device")):
    """变量模板（Grafana 式）：给定指标的维度可选值（供下拉切换）"""
    store = await get_store()
    out: dict[str, list[str]] = {}
    for dim in [d for d in dims.split(",") if d][:4]:
        rows = await store.metric_dim_breakdown(workspace_id, metric or "ga4_sessions",
                                                dim, days=90, limit=30)
        out[dim] = [str(r["key"]) for r in rows if r["key"]]
    return {"vars": out}


@app.get("/api/v1/estimate")
async def estimate_traffic(workspace_id: str = Query(...), domain: str = Query(""),
                           domains: str = Query(""), days: float = Query(30)):
    """竞品/自有域名相对流量指数（方法+置信度透明，不做绝对承诺）"""
    from ..engine.traffic_estimate import estimate_domain, estimate_many
    if domains:
        return {"estimates": await estimate_many(
            workspace_id, [d for d in domains.split(",") if d], days)}
    if not domain:
        raise HTTPException(status_code=400, detail="需要 domain 或 domains")
    return await estimate_domain(workspace_id, domain, days)


@app.get("/api/v1/ui/layout/history")
async def layout_history(workspace_id: str = Query(...), path: str = Query("")):
    """布局版本历史（协作可回溯到上一版）"""
    ws = await (await get_store()).get_workspace(workspace_id)
    history = list(((ws.settings_json or {}).get("layouts_history") or [])) if ws else []
    if path:
        history = [h for h in history if h.get("path") == path]
    return {"history": history[-20:][::-1]}


@app.get("/api/v1/auth/me")
async def whoami(request: Request):
    """当前身份与能力（前端据此隐藏无权限入口）"""
    from ..engine.permissions import MATRIX
    role = _role_of(request)
    return {"role": role, "capabilities": sorted(MATRIX.get(role, MATRIX["viewer"])),
            "user": getattr(request.state, "user", None) or {}}


@app.get("/api/v1/export/xlsx")
async def export_xlsx(workspace_id: str = Query(...), panel: str = Query(...),
                      days: float = Query(30)):
    """整页 Excel 导出：驾驶舱（或单指标）→ 多表工作簿

    把聚合结果按可读表拍平（KPI / 趋势 / 地域 / 渠道 …），交付给客户继续分析。
    """
    from urllib.parse import quote as _quote

    from fastapi.responses import Response

    from ..core.xlsx import write_xlsx
    from ..web.routes import _embed_normalize, build_export_sheets
    kind, _, name = panel.partition(":")
    if kind == "metric":
        series = await (await get_store()).metric_series(workspace_id, name, days=days)
        sheets = [("趋势", ["时间桶", "值", "样本数"],
                   [[r["bucket"], r["value"], r["n"]] for r in series])]
        stem = f"metric-{name}"
    elif kind == "cockpit":
        from ..web.cockpit import COCKPITS
        fn = COCKPITS.get(name)
        if not fn:
            raise HTTPException(status_code=404, detail="驾驶舱不存在")
        data = _embed_normalize(await fn(workspace_id, days))
        sheets = build_export_sheets(name, data)
        stem = f"cockpit-{name}"
    else:
        raise HTTPException(status_code=400, detail="panel 形如 cockpit:traffic")
    data_bytes = write_xlsx([(t, cols, rows) for t, cols, rows in sheets])
    return Response(
        content=data_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 f"attachment; filename={stem}-{time.strftime('%Y%m%d')}.xlsx; "
                 f"filename*=UTF-8''{_quote(stem)}.xlsx"})


@app.get("/api/v1/charts/range")
async def chart_range(workspace_id: str = Query(...), from_: str = Query("", alias="from"),
                      to: str = Query("")):
    """区间洞察：某时间窗内的洞察 + 动作（含验证结论）

    用途：框选时间轴 → 查看"这段时间发生了什么、动作验证结果如何"（服务闭环验证）。
    """
    import json as _json
    store = await get_store()
    lo = (from_ or "0000")[:10]
    hi = (to or "9999")[:10]
    insights = [i for i in await store.list_insights(workspace_id, limit=300)
                if lo <= i.created_at.strftime("%Y-%m-%d") <= hi]
    actions = []
    for a in await store.list_actions(workspace_id):
        ts = (a.dispatched_at or a.created_at).strftime("%Y-%m-%d")
        if lo <= ts <= hi:
            actions.append({
                "id": a.id, "action_type": a.action_type, "state": a.state.value,
                "verdict": (a.result_json or {}).get("verdict", ""),
                "insight_id": a.insight_id,
            })
    return {
        "from": from_, "to": to,
        "insights": [{"id": i.id, "type": i.type, "title": i.title,
                      "severity": i.severity.value,
                      "created_at": i.created_at.isoformat()} for i in insights[:40]],
        "actions": actions[:40],
    }


@app.get("/api/v1/charts/drill")
async def chart_drill(workspace_id: str = Query(...), metric: str = Query(""),
                      entity_id: str = Query(""), hours: int = Query(720),
                      limit: int = Query(60)):
    """图表下钻：某主体某指标的时间序列 + 关联洞察（P0 交互）"""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    import json as _json
    store = await get_store()
    since = (_dt.now(_tz.utc) - _td(hours=hours)).isoformat()

    rows: list[dict] = []
    if metric:
        params: list = [workspace_id, metric, since]
        where = "workspace_id = ? AND metric = ? AND ts >= ?"
        if entity_id:
            where += " AND entity_id = ?"
            params.append(entity_id)
        raw = await store._fetchall(
            f"SELECT ts, value, dim_json, monitor_id FROM metrics WHERE {where} "
            f"ORDER BY ts DESC LIMIT ?", tuple([*params, limit]))
        rows = [{"ts": r["ts"], "value": r["value"], "dim": _json.loads(r["dim_json"] or "{}"),
                 "monitor_id": r["monitor_id"]} for r in raw]

    insights = []
    if entity_id:
        all_ins = await store.list_insights(workspace_id, limit=200)
        for i in all_ins:
            blob = (i.title + " " + i.summary + " " + _json.dumps(i.evidence_json or [],
                                                                  ensure_ascii=False))
            if entity_id in blob:
                insights.append({"id": i.id, "type": i.type, "title": i.title,
                                 "severity": i.severity.value,
                                 "created_at": i.created_at.isoformat()})
    return {"metric": metric, "entity_id": entity_id, "rows": rows, "insights": insights[:20]}


@app.get("/api/v1/templates")
async def list_templates():
    """列出可用的行业模板包"""
    from ..engine.template_pack import TemplateRegistry
    return {"templates": TemplateRegistry().list()}


# ========== 洞察订阅（G-5）==========

class SubscriptionCreate(BaseModel):
    name: str
    channels: list[str]
    target: dict = {}
    filters: dict = {}
    mode: str = "immediate"


@app.get("/api/v1/subscriptions")
async def list_subscriptions(workspace_id: str = Query(...)):
    from ..engine.subscriptions import SubscriptionService
    return {"subscriptions": await SubscriptionService(workspace_id).list()}


@app.post("/api/v1/subscriptions")
async def create_subscription(workspace_id: str, data: SubscriptionCreate):
    from ..engine.subscriptions import SubscriptionError, SubscriptionService
    try:
        return await SubscriptionService(workspace_id).create(
            data.name, data.channels, data.target, data.filters, data.mode)
    except SubscriptionError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.delete("/api/v1/subscriptions/{subscription_id}")
async def delete_subscription(workspace_id: str, subscription_id: str):
    from ..engine.subscriptions import SubscriptionService
    ok = await SubscriptionService(workspace_id).delete(subscription_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return {"ok": True}


@app.post("/api/v1/subscriptions/{subscription_id}/toggle")
async def toggle_subscription(workspace_id: str, subscription_id: str, enabled: bool = Query(...)):
    from ..engine.subscriptions import SubscriptionService
    ok = await SubscriptionService(workspace_id).set_enabled(subscription_id, enabled)
    if not ok:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return {"ok": True, "enabled": enabled}


@app.get("/api/v1/audit/export")
async def audit_export(workspace_id: str = Query(...), format: str = Query("json"),
                       date_from: str | None = Query(None),
                       date_to: str | None = Query(None),
                       event_type: str | None = Query(None),
                       keyword: str | None = Query(None)):
    """审计导出（企业采购合规）"""
    from ..engine.audit import AuditExporter
    events = AuditExporter(workspace_id).collect(date_from, date_to, event_type, keyword)
    return {"count": len(events), "events": events}


@app.post("/api/v1/templates/apply")
async def apply_template(workspace_id: str, data: TemplateApplyRequest):
    """一键应用行业模板包（幂等）"""
    from ..engine.template_pack import TemplateRegistry
    store = await get_store()
    if not await store.get_workspace(workspace_id):
        raise HTTPException(status_code=404, detail="Workspace not found")
    pack = TemplateRegistry().get(data.template_id)
    if not pack:
        raise HTTPException(status_code=404, detail=f"模板不存在: {data.template_id}")
    return await pack.apply(workspace_id)


class WhiteLabelExportRequest(BaseModel):
    report_category: str
    filename: str
    client_name: str = ""
    company: str = ""
    logo_url: str = ""
    accent_color: str = "#2563eb"
    footer: str = ""
    disclaimer: str = ""


@app.post("/api/v1/reports/branded")
async def export_branded_report(workspace_id: str, data: WhiteLabelExportRequest):
    """白标报告导出（代理公司场景，套餐门控）"""
    from ..engine.white_label import WhiteLabelConfig, WhiteLabelRenderer
    renderer = WhiteLabelRenderer(
        workspace_id,
        WhiteLabelConfig(
            company=data.company, logo_url=data.logo_url,
            accent_color=data.accent_color,
            footer=data.footer, disclaimer=data.disclaimer,
        ),
    )
    result = await renderer.export(data.report_category, data.filename, client_name=data.client_name)
    if not result["ok"]:
        raise HTTPException(status_code=403 if "白标" in result["detail"] else 404, detail=result["detail"])
    return result


class WorkspaceCreateWithPlan(BaseModel):
    name: str
    stage: str = "S0"
    plan: str | None = None


# 原工作区创建端点升级：云托管建租户时按套餐校验 workspace 数上限
@app.post("/api/v1/workspaces")
async def create_workspace(data: WorkspaceCreateWithPlan):
    """创建工作区（云托管模式按套餐校验 workspace 数上限）"""

    # 多租户上限校验：统计现有 workspace 数与套餐上限对比（首租户不限制）
    from ..core.store import _store  # noqa
    if data.plan or _os.environ.get("INSFLOW_CLOUD") == "1":
        from ..engine.billing import PLANS
        if data.plan and data.plan not in PLANS:
            raise HTTPException(status_code=422, detail=f"未知套餐: {data.plan}")

    store = await get_store()
    ws = Workspace(
        name=data.name,
        stage=WorkspaceStage(data.stage),
    )
    if data.plan:
        ws.settings_json = {"plan": data.plan}
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
    from ..engine.subscriptions import notify_new_insight
    await notify_new_insight(data.workspace_id, insight.id)
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

# 内核 fail-closed（R2-4）：默认要求认证；本地调试显式 INSFLOW_MCP_AUTH=0 才放开
MCP_AUTH_REQUIRED = _os.environ.get("INSFLOW_MCP_AUTH", "1") != "0"


async def _mcp_auth_guard(request, action: str = "read"):
    if not MCP_AUTH_REQUIRED:
        return None
    return await require_auth(request, "read")


@app.get("/api/v1/mcp/tools")
async def mcp_list_tools(request: Request):
    """MCP 工具清单（10 个，与 stdio server 同源；认证默认强制）"""
    from ..mcp_server.tools import MCP_TOOLS_SCHEMA
    if MCP_AUTH_REQUIRED:
        await require_auth(request, "read")
    return {"tools": MCP_TOOLS_SCHEMA, "transport": "http",
            "auth_required": MCP_AUTH_REQUIRED}


class McpToolCall(BaseModel):
    name: str
    arguments: dict = {}


@app.post("/api/v1/mcp/tools/call")
async def mcp_call_tool(data: McpToolCall, request: Request):
    """MCP 工具调用（认证默认强制；触发类工具要求 write scope）"""
    from ..mcp_server.tools import call_mcp_tool
    if MCP_AUTH_REQUIRED:
        write_tools = {"trigger_playbook", "run_diagnosis"}
        await require_auth(request, "write" if data.name in write_tools else "read")
    import json as jsonlib
    output = await call_mcp_tool(data.name, data.arguments)
    try:
        return JSONResponse(content=jsonlib.loads(output))
    except (jsonlib.JSONDecodeError, TypeError):
        return {"raw": output}


# ========== 插件市场（M4）==========

@app.get("/api/v1/marketplace")
async def marketplace_scan():
    """浏览本地插件市场"""
    from ..engine.marketplace import Marketplace
    return {"plugins": Marketplace().scan()}


@app.get("/api/v1/plugins")
async def plugins_installed():
    from ..engine.marketplace import Marketplace
    return {"plugins": Marketplace().installed()}


class PluginInstallRequest(BaseModel):
    path: str


@app.post("/api/v1/plugins/install")
async def install_plugin(data: PluginInstallRequest):
    from ..engine.marketplace import Marketplace
    result = Marketplace().install(data.path)
    if not result["ok"]:
        raise HTTPException(status_code=422, detail=result)
    return result


# ========== 自定义规则 DSL（IM-3）/ 模型效果（IM-5）==========

class DSLRuleRequest(BaseModel):
    spec: dict


@app.post("/api/v1/models/dsl")
async def register_dsl_rule(workspace_id: str, data: DSLRuleRequest):
    """注册自定义规则模型（无代码配置）"""
    from ..engine.dsl_models import DSLValidationError, get_dsl_registry
    try:
        model = get_dsl_registry().register(workspace_id, data.spec)
        return {"ok": True, "rule_id": model.id, "name": model.name}
    except DSLValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.get("/api/v1/models/dsl")
async def list_dsl_rules(workspace_id: str = Query(...)):
    from ..engine.dsl_models import get_dsl_registry
    return {"rules": get_dsl_registry().list_for(workspace_id)}


@app.delete("/api/v1/models/dsl/{rule_id}")
async def delete_dsl_rule(workspace_id: str, rule_id: str):
    from ..engine.dsl_models import get_dsl_registry
    ok = get_dsl_registry().unregister(workspace_id, rule_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Rule not found")
    return {"ok": True}


@app.get("/api/v1/models/effectiveness")
async def model_effectiveness(workspace_id: str = Query(...)):
    """模型效果追踪（IM-5）：按模型聚合洞察命中率"""
    store = await get_store()
    if not await store.get_workspace(workspace_id):
        raise HTTPException(status_code=404, detail="Workspace not found")
    return {"models": await store.get_model_effectiveness(workspace_id)}


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


@app.post("/api/v1/reports/deep-dive")
async def build_deep_report(workspace_id: str = Query(...)):
    """深度报告生成器（多 Agent 协作，顾问场景交付物）"""
    from ..engine.deep_report import DeepReportBuilder
    store = await get_store()
    if not await store.get_workspace(workspace_id):
        raise HTTPException(status_code=404, detail="Workspace not found")
    builder = DeepReportBuilder(workspace_id)
    return await builder.build()


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
