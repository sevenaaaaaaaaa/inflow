"""Insight Flow Web 控制台（Jinja2 SSR，零构建链家族风格）

页面：仪表盘 / 洞察流 / 监控任务 / 报告中心 / 插件市场 / 套餐用量
服务端直读数据渲染，操作经原生 JS fetch 调 REST API（htmx 同构可替换）。
"""

from pathlib import Path

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import __version__
from ..core.files import ReportStore
from ..core.store import get_store

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _md_to_html_filter(md: str) -> str:
    """复用白标渲染器的极简 MD→HTML（零构建链）"""
    from ..engine.white_label import WhiteLabelRenderer
    return WhiteLabelRenderer("console")._md_to_html(md or "")


templates.env.filters["md_to_html"] = _md_to_html_filter

router = APIRouter(prefix="/console", include_in_schema=False)

CAT_NAMES = {
    "diagnosis": "流量诊断",
    "competitors": "竞品动向",
    "maturity": "成熟度",
    "verification": "验证报告",
    "weekly": "增长周报",
    "deep-dive": "深度报告",
}


async def _default_workspace() -> str:
    store = await get_store()
    wss = await store.list_workspaces()
    return wss[0].id if wss else "default"


NAV_AREA = {
    "dashboard": "overview",
    "onboarding": "monitor",
    "monitors": "monitor",
    "insights": "insight",
    "reports": "report",
    "plugins": "ecosystem",
    "usage": "settings",
    "subscriptions": "settings",
}


def _ctx(request: Request, nav: str, workspace_id: str, **extra) -> dict:
    return {
        "request": request,
        "nav": nav,
        "area": NAV_AREA.get(nav, "overview"),
        "version": __version__,
        "workspace_id": workspace_id,
        **extra,
    }


@router.get("", response_class=HTMLResponse)
async def dashboard(request: Request, workspace_id: str = Query("")):
    if not workspace_id:
        workspace_id = await _default_workspace()
    store = await get_store()
    ws = await store.get_workspace(workspace_id)
    insights = await store.list_insights(workspace_id, limit=8)
    all_insights = await store.list_insights(workspace_id, limit=200)
    status_counts = {}
    for i in all_insights:
        status_counts[i.status.value] = status_counts.get(i.status.value, 0) + 1
    feedback = await store.get_feedback_stats(workspace_id)

    return templates.TemplateResponse(request, "dashboard.html", _ctx(
        request, "dashboard", workspace_id,
        stage=ws.stage.value if ws else "-",
        counts={
            "total": len(all_insights),
            "new": status_counts.get("new", 0),
            "actioned": status_counts.get("actioned", 0),
            "verified": status_counts.get("verified", 0),
        },
        feedback_effective=feedback.get("effective", 0),
        recent_insights=insights,
    ))


@router.get("/insights", response_class=HTMLResponse)
async def insights_page(request: Request, workspace_id: str = Query(""),
                        severity: str = Query(""), status: str = Query(""),
                        page: int = Query(1, ge=1)):
    if not workspace_id:
        workspace_id = await _default_workspace()
    store = await get_store()
    page_size = 25
    offset = (page - 1) * page_size
    insights = await store.list_insights(
        workspace_id, status=status or None, severity=severity or None,
        limit=page_size, offset=offset)
    total = await store.count_insights(workspace_id, status=status or None,
                                       severity=severity or None)
    pages = (total + page_size - 1) // page_size or 1
    return templates.TemplateResponse(request, "insights.html", _ctx(
        request, "insights", workspace_id,
        insights=insights, severity=severity, status=status,
        page=page, pages=pages, total=total,
    ))


@router.get("/monitors", response_class=HTMLResponse)
async def monitors_page(request: Request, workspace_id: str = Query("")):
    if not workspace_id:
        workspace_id = await _default_workspace()
    from ..engine.monitors import MonitorService
    monitors = await MonitorService(workspace_id).list()
    return templates.TemplateResponse(request, "monitors.html", _ctx(
        request, "monitors", workspace_id, monitors=monitors,
    ))


@router.get("/reports", response_class=HTMLResponse)
async def reports_page(request: Request, workspace_id: str = Query("")):
    if not workspace_id:
        workspace_id = await _default_workspace()
    store = ReportStore(workspace_id)
    all_reports = {cat: [p.name for p in store.list_reports(cat)]
                   for cat in ("weekly", "diagnosis", "competitors", "maturity", "verification")}
    deep = [p.name for p in store.list_reports("deep-dive")]
    return templates.TemplateResponse(request, "reports.html", _ctx(
        request, "reports", workspace_id,
        all_reports=all_reports, deep_reports=deep, cat_names=CAT_NAMES,
    ))


@router.get("/reports/view", response_class=HTMLResponse)
async def view_report(request: Request, category: str, filename: str,
                      workspace_id: str = Query("")):
    if not workspace_id:
        workspace_id = await _default_workspace()
    store = ReportStore(workspace_id)
    content = store.get_report(category, filename) or "报告不存在"
    return templates.TemplateResponse(request, "report_view.html", _ctx(
        request, "reports", workspace_id,
        category=category, filename=filename, content=content,
        cat_name=CAT_NAMES.get(category, category),
    ))


@router.get("/plugins", response_class=HTMLResponse)
async def plugins_page(request: Request, workspace_id: str = Query("")):
    if not workspace_id:
        workspace_id = await _default_workspace()
    from ..engine.marketplace import Marketplace
    market = Marketplace(workspace_id)
    return templates.TemplateResponse(request, "plugins.html", _ctx(
        request, "plugins", workspace_id,
        installed=market.installed(), market=market.scan(),
    ))


@router.get("/subscriptions", response_class=HTMLResponse)
async def subscriptions_page(request: Request, workspace_id: str = Query("")):
    if not workspace_id:
        workspace_id = await _default_workspace()
    from ..engine.subscriptions import SubscriptionService
    subs = await SubscriptionService(workspace_id).list()
    return templates.TemplateResponse(request, "subscriptions.html", _ctx(
        request, "subscriptions", workspace_id, subs=subs,
    ))


@router.get("/usage", response_class=HTMLResponse)
async def usage_page(request: Request, workspace_id: str = Query("")):
    if not workspace_id:
        workspace_id = await _default_workspace()
    from ..engine.billing import PLANS, BillingManager
    summary = await BillingManager(workspace_id).usage_summary()
    return templates.TemplateResponse(request, "usage.html", _ctx(
        request, "usage", workspace_id,
        plan_id=summary["plan_id"],
        plan_name=summary["plan_name"],
        price=summary["price_usd_month"],
        features=summary["features"],
        sources_allowed=summary["sources_allowed"],
        usage=summary["usage"],
        checked_at=summary["checked_at"],
        all_plans=PLANS,
    ))


@router.get("/onboarding", response_class=HTMLResponse)
async def onboarding_page(request: Request, workspace_id: str = Query("")):
    if not workspace_id:
        workspace_id = await _default_workspace()
    from ..engine.onboarding import OnboardingService
    svc = OnboardingService(workspace_id)
    health = []
    for provider in ("gsc", "ga4", "crux"):
        health.append(await svc.check_health(provider))
    return templates.TemplateResponse(request, "onboarding.html", _ctx(
        request, "onboarding", workspace_id, health=health,
    ))


@router.get("/", include_in_schema=False)
async def console_root():
    return RedirectResponse("/console")


# ========== 账号（R4-1 多租户自助）==========



@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = Query(""), next: str = Query("/console")):
    return templates.TemplateResponse(request, "login.html", {
        "request": request, "version": __version__, "error": error, "next": next,
        "saas": _SAAS_MODE(),
    })


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, email: str = Form(...), password: str = Form(...),
                       next: str = Form("/console")):
    from ..core.accounts import COOKIE_NAME, AccountError, AccountManager
    try:
        result = await AccountManager().login(email, password)
    except AccountError as e:
        return RedirectResponse(f"/console/login?error={e}", status_code=303)
    resp = RedirectResponse(next or "/console", status_code=303)
    resp.set_cookie(COOKIE_NAME, result["token"], httponly=True, samesite="lax",
                    max_age=30 * 86400)
    return resp


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, error: str = Query("")):
    return templates.TemplateResponse(request, "register.html", {
        "request": request, "version": __version__, "error": error,
    })


@router.post("/register", response_class=HTMLResponse)
async def register_submit(request: Request, email: str = Form(...),
                          password: str = Form(...), name: str = Form(""),
                          workspace_name: str = Form("")):
    from ..core.accounts import COOKIE_NAME, AccountError, AccountManager
    try:
        result = await AccountManager().register(email, password, name, workspace_name)
    except AccountError as e:
        return RedirectResponse(f"/console/register?error={e}", status_code=303)
    resp = RedirectResponse("/console/onboarding", status_code=303)
    resp.set_cookie(COOKIE_NAME, result["token"], httponly=True, samesite="lax",
                    max_age=30 * 86400)
    return resp


@router.get("/logout")
async def logout(request: Request):
    from ..core.accounts import COOKIE_NAME, AccountManager
    await AccountManager().logout(request.cookies.get(COOKIE_NAME))
    resp = RedirectResponse("/console/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME)
    return resp


def _SAAS_MODE() -> bool:
    import os
    return os.environ.get("INSFLOW_SAAS", "") == "1"
