"""Insight Flow Web 控制台（Jinja2 SSR，零构建链家族风格）

页面：仪表盘 / 洞察流 / 监控任务 / 报告中心 / 插件市场 / 套餐用量
服务端直读数据渲染，操作经原生 JS fetch 调 REST API（htmx 同构可替换）。
"""

import os
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
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

from ..viz import charts as _viz  # noqa: E402
from ..viz import frame as _viz_frame  # noqa: E402


class _VizNS:
    """模板命名空间：charts.* + frame.datapanel（模板统一用 viz.xxx）"""

    def __init__(self):
        for name in dir(_viz):
            if not name.startswith("_"):
                setattr(self, name, getattr(_viz, name))
        self.datapanel = _viz_frame.datapanel
        from ..viz.base import TOKENS_CSS
        self.tokens_css = TOKENS_CSS


templates.env.globals["viz"] = _VizNS()
# 模板常用过滤器（Jinja 无内置 zip）
templates.env.filters["zip"] = lambda *seqs: [list(t) for t in zip(*seqs)]

router = APIRouter(prefix="/console", include_in_schema=False)

CAT_NAMES = {
    "diagnosis": "流量诊断",
    "competitors": "竞品动向",
    "maturity": "成熟度",
    "verification": "验证报告",
    "weekly": "增长周报",
    "deep-dive": "深度报告",
}


def build_board_panels(name: str, data: dict, *, limit: int = 24) -> list[dict]:
    """驾驶舱数据 → 面板列表（图 + 数据表同源）

    返回 [{title, columns, rows, chart, csv}]；嵌入视图（单面板/整看板）复用同一构建。
    优先级：地域 > 渠道 > 风险 > 缺口 > 漏斗 > 留存 > 模型有效性 > 配额 >
    报告产出 > 待验证动作 > 竞品档案 > 结论 > 趋势。
    """
    from ..viz import charts as _c

    def _pairs(key: str) -> list:
        items = data.get(key) or []
        return [r for r in items if isinstance(r, (list, tuple)) and len(r) >= 2]

    panels: list[dict] = []
    if _pairs("geo"):
        panels.append({"title": "会话来源地域", "columns": ["地域", "值"],
                       "rows": [list(r) for r in _pairs("geo")],
                       "chart": _c.tile_map(_pairs("geo"),
                                            scope=data.get("geo_scope") or "china",
                                            unit=" 会话",
                                            label="近似地理网格，非精确边界"),
                       "csv": name + "-geo"})
    box = data.get("box") or []
    if box and isinstance(box[0], (list, tuple)):
        pairs = [(str(b[0]), list(b[1])) for b in box]
        panels.append({"title": "渠道转化分布（箱线）", "columns": ["渠道", "样本数"],
                       "rows": [[k, len(v)] for k, v in pairs],
                       "chart": _c.boxplot(pairs, y_label="转化量"),
                       "csv": name + "-box"})
    if _pairs("channels"):
        panels.append({"title": "渠道构成", "columns": ["渠道", "值"],
                       "rows": [list(r) for r in _pairs("channels")],
                       "chart": _c.percent_bar(_pairs("channels")),
                       "csv": name + "-channels"})
    if _pairs("risk"):
        panels.append({"title": "各主体负向占比", "columns": ["主体", "负向占比"],
                       "rows": [list(r) for r in _pairs("risk")],
                       "chart": _c.bar_chart(_pairs("risk"), threshold=0.35,
                                             threshold_label="预警线",
                                             value_fmt=_c.fmt_pct),
                       "csv": name + "-risk"})
    if _pairs("gaps"):
        panels.append({"title": "关键词缺口（搜索量）", "columns": ["关键词", "搜索量"],
                       "rows": [list(r) for r in _pairs("gaps")],
                       "chart": _c.bar_chart(_pairs("gaps")),
                       "csv": name + "-gaps"})
    if _pairs("funnel"):
        panels.append({"title": "旅程漏斗", "columns": ["阶段", "到达数"],
                       "rows": [list(r) for r in _pairs("funnel")],
                       "chart": _c.funnel(_pairs("funnel")),
                       "csv": name + "-funnel"})
    retention = data.get("retention") or []
    if retention:
        panels.append({"title": "留存趋势", "columns": ["时间", "留存"],
                       "rows": [[r.get("bucket"), r.get("value")] for r in retention],
                       "chart": _c.line_chart(
                           [{"name": "retention",
                             "values": [r.get("value") for r in retention]}],
                           [r.get("bucket") for r in retention], as_area=True,
                           anomaly=True),
                       "csv": name + "-retention"})
    eff = data.get("effectiveness") or []
    if eff:
        pairs = [(str(e.get("model", ""))[:18], e.get("samples", 0)) for e in eff]
        panels.append({"title": "模型有效性", "columns": ["模型", "样本数", "命中率"],
                       "rows": [[e.get("model"), e.get("samples"), e.get("hit_rate")]
                                for e in eff],
                       "chart": _c.bar_chart(pairs),
                       "csv": name + "-effectiveness"})
    if data.get("gauges"):
        panels.append({"title": "用量水位", "columns": ["指标", "占比"],
                       "rows": [[k, v] for k, v in data["gauges"]],
                       "chart": "".join(_c.gauge(v or 0, label=k)
                                        for k, v in data["gauges"]),
                       "csv": name + "-quota"})
    if _pairs("quota"):
        panels.append({"title": "数据源配额", "columns": ["来源", "已用", "上限", "窗口"],
                       "rows": [[r[0], r[1] if len(r) > 1 else "", r[2] if len(r) > 2 else "",
                                 r[3] if len(r) > 3 else ""] for r in _pairs("quota")],
                       "chart": '<div class="empty">配额明细见数据表</div>',
                       "csv": name + "-quota-detail"})
    if isinstance(data.get("categories"), dict) and data["categories"]:
        rows = [[k, len(v)] for k, v in data["categories"].items()]
        panels.append({"title": "报告产出", "columns": ["类别", "份数"], "rows": rows,
                       "chart": _c.bar_chart(rows), "csv": name + "-reports"})
    if data.get("due"):
        panels.append({"title": "待验证动作", "columns": ["动作", "工作区"],
                       "rows": [[str(getattr(a, "action_type", a)),
                                 str(getattr(a, "workspace_id", ""))] for a in data["due"]],
                       "chart": '<div class="empty">待验证清单见数据表</div>',
                       "csv": name + "-due"})
    if data.get("profiles"):
        rows = [[str(getattr(x, "name", "") or ""), str(getattr(x, "domain", ""))
                 if not isinstance(x, dict) else x.get("domain", "")]
                for x in data["profiles"]]
        panels.append({"title": "竞品档案", "columns": ["名称", "域名"], "rows": rows,
                       "chart": '<div class="empty">档案见数据表</div>',
                       "csv": name + "-profiles"})
    if data.get("verdicts"):
        vd = data["verdicts"]
        rows = [["有效", vd.get("effective", 0)], ["无效", vd.get("ineffective", 0)],
                ["待验证", vd.get("pending", 0)]]
        panels.append({"title": "动作验证结论", "columns": ["结论", "数量"], "rows": rows,
                       "chart": _c.percent_bar([("有效", vd.get("effective", 0), "var(--ok)"),
                                                ("无效", vd.get("ineffective", 0), "var(--danger)"),
                                                ("待验证", vd.get("pending", 0), "var(--muted)")]),
                       "csv": name + "-verdicts"})
    trend = data.get("trend")
    if isinstance(trend, dict) and trend.get("labels"):
        keys = [k for k, v in trend.items() if k != "labels" and isinstance(v, list)
                and v and any(x is not None for x in v)]
        if keys:
            rows = []
            for i, lab in enumerate(trend["labels"]):
                rows.append([lab] + [(trend[k][i] if i < len(trend[k]) else None)
                                     for k in keys])
            panels.append({"title": f"{name} 趋势", "columns": ["时间"] + keys,
                           "rows": rows,
                           "chart": _c.line_chart(
                               [{"name": k, "values": trend[k]} for k in keys],
                               trend["labels"], as_area=(len(keys) == 1),
                               anomaly=True, forecast_periods=7),
                           "csv": name + "-trend"})
    if data.get("rows"):
        rows = data["rows"]
        panels.append({"title": name, "columns": ["时间桶", "值"],
                       "rows": [[r.get("bucket"), r.get("value")] for r in rows],
                       "chart": _c.line_chart(
                           [{"name": name, "values": [r.get("value") for r in rows]}],
                           [r.get("bucket") for r in rows], as_area=True,
                           anomaly=True, forecast_periods=7),
                       "csv": name + "-series"})
    if data.get("timeline"):
        panels.append({"title": "最近事件", "columns": ["时间", "事件", "来源"],
                       "rows": [[t.get("ts"), t.get("title"), t.get("meta")]
                                for t in data["timeline"]],
                       "chart": '<div class="empty">事件列表</div>',
                       "csv": name + "-timeline"})
    return panels[:limit]


def build_export_sheets(name: str, data: dict) -> list[tuple[str, list, list]]:
    """驾驶舱数据 → Excel 多表（KPI / 趋势 / 分布 / 表格类字段）"""
    sheets: list[tuple[str, list, list]] = []
    kpis = data.get("kpis")
    if isinstance(kpis, dict) and kpis:
        sheets.append((f"{name}-KPI", ["指标", "值"],
                       [[k, v] for k, v in kpis.items()]))
    trend = data.get("trend")
    if isinstance(trend, dict) and trend.get("labels"):
        cols = ["时间"] + [k for k, v in trend.items()
                           if k != "labels" and isinstance(v, list)]
        rows = []
        for i, lab in enumerate(trend["labels"]):
            row = [lab]
            for k in cols[1:]:
                vals = trend.get(k) or []
                row.append(vals[i] if i < len(vals) else None)
            rows.append(row)
        sheets.append((f"{name}-趋势", cols, rows))
    for key, cols in (("geo", ["地域", "值"]), ("channels", ["渠道", "值"]),
                      ("risk", ["主体", "负向占比"]), ("gaps", ["关键词", "搜索量"]),
                      ("profiles", ["名称", "域名"]),
                      ("quota", ["来源", "已用", "上限", "窗口"])):
        items = data.get(key)
        if isinstance(items, list) and items and isinstance(items[0], (list, tuple)):
            width = max(len(r) for r in items if isinstance(r, (list, tuple)))
            header = cols[:width] if len(cols) >= width else cols + [
                f"列{i}" for i in range(len(cols), width)]
            sheets.append((f"{name}-{key}", header,
                           [list(r) for r in items]))
    retention = data.get("retention")
    if isinstance(retention, list) and retention:
        sheets.append((f"{name}-留存", ["时间", "值"],
                       [[r.get("bucket"), r.get("value")] for r in retention]))
    funnel = data.get("funnel")
    if isinstance(funnel, list) and funnel:
        sheets.append((f"{name}-漏斗", ["阶段", "到达数"],
                       [list(r) for r in funnel]))
    cats = data.get("categories")
    if isinstance(cats, dict) and cats:
        sheets.append((f"{name}-报告产出", ["类别", "份数"],
                       [[k, len(v)] for k, v in cats.items()]))
    if not sheets:
        sheets.append((f"{name}", ["说明"],
                       [["该面板暂无可导出表格数据"]]))
    return sheets


def _embed_normalize(data: dict | None) -> dict:
    """嵌入视图数据规范化：只保留可安全渲染的标量/元组，避免模板解包异构对象"""
    if not isinstance(data, dict):
        return {}
    out = dict(data)
    gaps = out.get("gaps")
    if isinstance(gaps, list) and gaps and not isinstance(gaps[0], (list, tuple)):
        out["gaps"] = [(getattr(g, "title", str(g))[:24], 1) for g in gaps]
    kpis = out.get("kpis")
    if isinstance(kpis, dict):
        out["kpis"] = {k: v for k, v in kpis.items()
                       if isinstance(v, (int, float, str)) and not isinstance(v, bool)}
    return out


def cross_filters(request: Request) -> dict:
    """URL 中的 cf.<dim>=<value> → 维度过滤（cross-filter 联动）"""
    out: dict[str, str] = {}
    for k, v in request.query_params.items():
        if k.startswith("cf.") and v:
            out[k[3:]] = v
    return out


def template_vars(request: Request) -> dict:
    """URL 中的 var.<dim>=<value> → 变量模板（Grafana 式看板变量）"""
    out: dict[str, str] = {}
    for k, v in request.query_params.items():
        if k.startswith("var.") and v:
            out[k[4:]] = v
    return out


async def _default_workspace() -> str:
    store = await get_store()
    wss = await store.list_workspaces()
    return wss[0].id if wss else "default"


NAV_AREA = {
    "dashboard": "overview",
    "onboarding": "monitor",
    "monitors": "monitor",
    "insights": "insight",
    "sentiment": "insight",
    "explore": "insight",
    "reports": "report",
    "plugins": "ecosystem",
    "usage": "settings",
    "subscriptions": "settings",
    "ops": "settings",
    "overview": "overview",
    "traffic": "insight",
    "action-loop": "loop",
    "competitor": "growth",
    "journey": "growth",
}


# 各页面实时流订阅的指标（SSE metrics 频道）
LIVE_METRICS = {
    "traffic": ["ga4_sessions", "gsc_clicks", "ga4_conversions"],
    "sentiment": ["topic_negative_ratio"],
    "overview": ["ga4_sessions", "topic_negative_ratio"],
    "action-loop": ["ga4_conversions"],
    "journey": ["ga4_retention", "ga4_conversions"],
    "explore": ["ga4_sessions"],
}


def _role_ctx(request: Request, settings: dict | None
              ) -> tuple[str, list[str], set]:
    """角色 + 行级白名单 + 能力集（模板据此隐藏入口；查询据此收敛数据）"""
    user = getattr(request.state, "user", None)
    role = str((user or {}).get("role") or "owner")
    from ..engine.permissions import MATRIX, entity_allow
    caps = MATRIX.get(role, MATRIX["viewer"])
    return role, entity_allow(settings, role), set(caps)


def _policy_ctx(request: Request, settings: dict | None):
    """表达式级 RLS：角色策略 → (WHERE 片段, 参数)"""
    from ..engine.rls import build_policy
    user = getattr(request.state, "user", None) or {}
    role = str(user.get("role") or "owner")
    return build_policy(settings, role, user)


def _ctx(request: Request, nav: str, workspace_id: str, **extra) -> dict:
    return {
        "request": request,
        "nav": nav,
        "area": NAV_AREA.get(nav, "overview"),
        "version": __version__,
        "workspace_id": workspace_id,
        # base.html 会被子模板 import，那里没有 request，故在此预计算
        "cf_active": list(cross_filters(request).items()),
        "live_metrics": LIVE_METRICS.get(nav, []),
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


@router.get("/reports/visual", response_class=HTMLResponse)
async def report_visual(request: Request, category: str, filename: str,
                        workspace_id: str = Query("")):
    """可视化报告（MD → 注入 SVG 图表的自包含 HTML，可打印 PDF）"""
    if not workspace_id:
        workspace_id = await _default_workspace()
    from ..engine.report_render import ReportRenderer
    try:
        html = await ReportRenderer(workspace_id).render(category, filename)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    # 报告查看：水印 + 访问审计（企业合规）
    from ..engine.watermark import ACTIONS, actor_of, company_of, stamp
    actor = actor_of(request)
    mark = stamp(actor, await company_of(workspace_id))
    html = html.replace("</body>",
                        f'<div style="padding:10px 16px;font-size:11px;color:#888">'
                        f'{mark}</div></body>') if "</body>" in html else \
        html + f'<div style="font-size:11px;color:#888">{mark}</div>'
    try:
        store = await get_store()
        await store.record_admin(workspace_id, ACTIONS["report"], actor=actor,
                                 target_type="report", target_id=filename,
                                 detail={"category": category})
    except Exception:
        pass
    return HTMLResponse(html)


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


COCKPIT_PAGES = {
    "overview": ("overview.html", "overview", "情报总览"),
    "traffic": ("traffic.html", "traffic", "流量驾驶舱"),
    "action-loop": ("action_loop.html", "action-loop", "行动验证驾驶舱"),
    "competitor": ("competitor.html", "competitor", "竞品驾驶舱"),
    "journey": ("journey.html", "journey", "客户旅程驾驶舱"),
    "ops": ("ops.html", "ops", "运维监控驾驶舱"),
    "billing": ("billing.html", "billing", "商业化驾驶舱"),
}


@router.get("/cockpit/{name}", response_class=HTMLResponse)
async def cockpit_page(request: Request, name: str, workspace_id: str = Query(""),
                       days: float = Query(0), entity: str = Query(""),
                       channel: str = Query("")):
    """驾驶舱统一入口（9 舱，TTL 缓存聚合）"""
    if name not in COCKPIT_PAGES:
        return RedirectResponse("/console")
    if not workspace_id:
        workspace_id = await _default_workspace()
    from .cockpit import COCKPITS
    fn = COCKPITS[name]
    # 变量模板并入联动筛选（同为维度等值过滤）
    cf = {**template_vars(request), **cross_filters(request)}
    ws_row = await (await get_store()).get_workspace(workspace_id)
    _settings = (ws_row.settings_json if ws_row else {}) or {}
    role, allow, caps = _role_ctx(request, _settings)
    policy = _policy_ctx(request, _settings)
    if allow and entity and entity not in allow:
        return templates.TemplateResponse(request, "403.html", _ctx(
            request, nav, workspace_id, title="无权限"), status_code=403)
    kw: dict = {}
    if name in ("traffic", "sentiment"):
        if allow:
            kw["entity_allow"] = allow
        if policy and policy[0]:
            kw["policy"] = policy
    if days <= 0:
        data = await fn(workspace_id, dim_filters=cf, **kw) if name == "traffic" \
            else await fn(workspace_id, **kw) if kw else await fn(workspace_id)
    elif name == "traffic":
        data = await fn(workspace_id, days, channel=channel, entity=entity,
                        dim_filters=cf, **kw)
    elif name == "sentiment":
        data = await fn(workspace_id, days, channel=channel, entity=entity, **kw)
    else:
        data = await fn(workspace_id, days)
    # 地图数据集选择：按维度值匹配度挑（内置 world / 用户导入），命中 0 则回退网格地图
    geo_dataset, geo_dataset_name = None, ""
    if name == "traffic":
        from ..engine.geo import list_datasets, load_dataset, pick_dataset
        names = list_datasets(_settings)
        if names:
            loaded = {}
            for ds_name in names:
                loaded[ds_name] = await load_dataset(workspace_id, ds_name)
            geo_dataset_name, geo_dataset = pick_dataset(
                names, loaded, [k for k, _ in (data.get("geo") or [])])
            if geo_dataset is None:
                geo_dataset_name = ""
    template, nav, title = COCKPIT_PAGES[name]
    dq_bad = ((data or {}).get("dq") or {}).get("bad") if name == "ops" else None
    return templates.TemplateResponse(request, template, _ctx(
        request, nav, workspace_id, data=data, title=title, days=days or 14,
        entity=entity, channel=channel, geo_dataset=geo_dataset,
        geo_dataset_name=geo_dataset_name, geo_dim=(data.get("geo_dim") or "province"),
        dq_bad=dq_bad, dq=((data or {}).get("dq") if name == "ops" else None),
    ))


@router.get("/sentiment", response_class=HTMLResponse)
async def sentiment_page(request: Request, workspace_id: str = Query(""),
                         days: float = Query(14), entity: str = Query(""),
                         channel: str = Query("")):
    """舆情驾驶舱（G-3）：情绪分布 + 负面预警 + 主体热力"""
    if not workspace_id:
        workspace_id = await _default_workspace()
    from .cockpit import COCKPITS
    ws_row = await (await get_store()).get_workspace(workspace_id)
    _settings = (ws_row.settings_json if ws_row else {}) or {}
    role, allow, caps = _role_ctx(request, _settings)
    policy = _policy_ctx(request, _settings)
    data = await COCKPITS["sentiment"](workspace_id, days, channel=channel,
                                       entity=entity, entity_allow=allow,
                                       policy=policy)
    return templates.TemplateResponse(request, "sentiment.html", _ctx(
        request, "sentiment", workspace_id, data=data, title="舆情驾驶舱", days=days,
        entity=entity, channel=channel,
    ))


@router.get("/m", response_class=HTMLResponse)
async def mobile_view(request: Request, workspace_id: str = Query("")):
    """移动端只读快照（PWA 离线可用的最小页面）"""
    if not workspace_id:
        workspace_id = await _default_workspace()
    store = await get_store()
    totals = await store.metric_totals(
        workspace_id, ["ga4_sessions", "gsc_clicks", "ga4_conversions"], days=7)
    kpis = []
    labels = {"ga4_sessions": "会话（7 天）", "gsc_clicks": "点击（7 天）",
              "ga4_conversions": "转化（7 天）"}
    for metric in ("ga4_sessions", "gsc_clicks", "ga4_conversions"):
        value = (totals.get(metric) or {}).get("value", 0)
        series = await store.metric_series(workspace_id, metric, days=7)
        kpis.append({
            "label": labels[metric], "display": f"{value:,.0f}",
            "spark_note": ("近 7 天 " + "·".join(
                f"{p['bucket'][-5:]}:{p['value']:,.0f}" for p in series[-3:])
                if series else "暂无数据"),
        })
    insights = await store.list_insights(workspace_id, limit=5)
    snapshot = {"at": __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc).isoformat(),
        "workspace_id": workspace_id, "kpis": kpis,
        "insights": [{"title": i.title[:60], "severity": i.severity.value,
                      "summary": i.summary[:110]} for i in insights]}
    return templates.TemplateResponse(request, "m.html", _ctx(
        request, "dashboard", workspace_id, kpis=kpis, insights=insights,
        snapshot=snapshot, offline_note=""))


@router.get("/snapshots", response_class=HTMLResponse)
async def snapshots_page(request: Request, workspace_id: str = Query("")):
    """快照归档（自包含 HTML，可直接打印 PDF）"""
    if not workspace_id:
        workspace_id = await _default_workspace()
    from ..engine.snapshot import list_snapshots
    items = list_snapshots(workspace_id)
    return templates.TemplateResponse(request, "snapshots.html", _ctx(
        request, "reports", workspace_id, items=items,
    ))


@router.get("/snapshots/{path:path}", response_class=HTMLResponse)
async def snapshot_file(request: Request, path: str, workspace_id: str = Query("")):
    """快照文件（含 PDF）；按工作区隔离 + 防目录穿越"""
    if not workspace_id:
        workspace_id = await _default_workspace()
    from fastapi.responses import FileResponse
    from ..engine.snapshot import resolve_snapshot
    # path 形如 "<workspace>/<file>"（由引擎生成的相对路径）
    name = path.split("/", 1)[1] if "/" in path else path
    target = resolve_snapshot(workspace_id, name)
    if not target:
        raise HTTPException(status_code=404, detail="快照不存在")
    media = "application/pdf" if target.suffix == ".pdf" else "text/html; charset=utf-8"
    return FileResponse(str(target), media_type=media)


@router.get("/audit", response_class=HTMLResponse)
async def audit_page(request: Request, workspace_id: str = Query(""),
                     action: str = Query(""), actor: str = Query("")):
    """管理动作审计（合规：谁在何时改了什么；可导出 CSV）"""
    if not workspace_id:
        workspace_id = await _default_workspace()
    store = await get_store()
    logs = await store.list_admin_audit(workspace_id, action=action, actor=actor,
                                        limit=300)
    return templates.TemplateResponse(request, "audit.html", _ctx(
        request, "audit", workspace_id, logs=logs, action=action, actor=actor,
    ))


@router.get("/sso/login", include_in_schema=False)
async def sso_login(request: Request, next: str = Query("/console")):
    """SSO 登录入口（OIDC 授权码流程；未配置则明确拒绝）"""
    from ..engine import sso
    if not sso.enabled():
        return HTMLResponse(
            '<div style="font:14px/1.7 system-ui;padding:24px">'
            '未启用 SSO：请配置 INSFLOW_OIDC_ISSUER / INSFLOW_OIDC_CLIENT_ID '
            '（可选 CLIENT_SECRET / SCOPES / DEFAULT_ROLE）</div>', status_code=400)
    state = sso.new_state()
    base = os.environ.get("INSFLOW_BASE_PATH", "")
    redirect_uri = f"{sso_public_base(request)}{base}/console/sso/callback"
    try:
        url = await sso.authorize_url(redirect_uri, state)
    except sso.SsoError as e:
        return HTMLResponse(f'<div style="padding:24px">SSO 配置错误：{e}</div>',
                            status_code=400)
    resp = RedirectResponse(url, status_code=303)
    resp.set_cookie(sso.state_cookie_name(), state, httponly=True, samesite="lax",
                    max_age=300, secure=request.url.scheme == "https")
    resp.set_cookie("if_oidc_next", next[:200], httponly=True, samesite="lax",
                    max_age=300, secure=request.url.scheme == "https")
    return resp


def sso_public_base(request: Request) -> str:
    """回调地址的公网前缀（反代场景由 X-Forwarded-* 决定）"""
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") \
        or request.url.netloc
    return f"{proto}://{host}"


@router.get("/sso/callback", include_in_schema=False)
async def sso_callback(request: Request, code: str = Query(""), state: str = Query(""),
                       error: str = Query("")):
    """SSO 回调：校验 state → 换 token → userinfo → 建会话"""
    from ..core.accounts import COOKIE_NAME
    from ..engine import sso
    if error:
        return HTMLResponse(f'<div style="padding:24px">IdP 返回错误：{error}</div>',
                            status_code=400)
    expect = request.cookies.get(sso.state_cookie_name(), "")
    if not code or not state or not expect or state != expect:
        return HTMLResponse('<div style="padding:24px">state 校验失败（请重新登录）</div>',
                            status_code=400)
    base = os.environ.get("INSFLOW_BASE_PATH", "")
    redirect_uri = f"{sso_public_base(request)}{base}/console/sso/callback"
    try:
        tokens = await sso.exchange_code(code, redirect_uri)
        userinfo = await sso.fetch_userinfo(tokens["access_token"])
        user = await sso.upsert_user(await _default_workspace(), userinfo)
        session = await sso.start_session(user["user_id"])
    except Exception as e:                      # 任何失败都不放行
        return HTMLResponse(f'<div style="padding:24px">SSO 登录失败：{e}</div>',
                            status_code=401)
    nxt = request.cookies.get("if_oidc_next") or f"{base}/console"
    resp = RedirectResponse(nxt, status_code=303)
    resp.set_cookie(COOKIE_NAME, session, httponly=True, samesite="lax",
                    max_age=14 * 24 * 3600, secure=request.url.scheme == "https")
    resp.delete_cookie(sso.state_cookie_name())
    return resp


@router.get("/manifest.webmanifest")
async def manifest():
    """PWA 清单（可"添加到主屏幕"；私有化内网同样可用）"""
    return JSONResponse({
        "name": "Insight Flow 增长情报", "short_name": "insFlow",
        "start_url": f"{os.environ.get('INSFLOW_BASE_PATH', '')}/console/m",
        "display": "standalone", "background_color": "#0f1115", "theme_color": "#2563eb",
        "description": "全域增长情报与洞察→动作→验证闭环",
        "shortcuts": [
            {"name": "数据监测", "url": f"{os.environ.get('INSFLOW_BASE_PATH', '')}/console/monitors"},
            {"name": "洞察流", "url": f"{os.environ.get('INSFLOW_BASE_PATH', '')}/console/insights"},
            {"name": "行动验证", "url": f"{os.environ.get('INSFLOW_BASE_PATH', '')}/console/cockpit/action-loop"},
        ],
        "icons": [{"src": "/console/icon.svg", "sizes": "any",
                   "type": "image/svg+xml", "purpose": "any"}],
    }, media_type="application/manifest+json")


@router.get("/icon.svg")
async def icon():
    """内联应用图标（零静态资源目录，SVG 任意尺寸）"""
    from fastapi.responses import Response
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
           '<rect width="64" height="64" rx="14" fill="#2563eb"/>'
           '<path d="M14 44 L26 30 L34 37 L50 18" fill="none" stroke="#fff" '
           'stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>'
           '<circle cx="50" cy="18" r="5" fill="#fff"/></svg>')
    return Response(svg, media_type="image/svg+xml")


@router.get("/sw.js")
async def service_worker():
    """Service Worker：离线外壳（文档页 network-first，静态图缓存）"""
    from fastapi.responses import Response
    js = r"""
const CACHE = 'insflow-shell-v2';
const SNAPSHOT = 'insflow-m-snapshot-v1';
self.addEventListener('install', e => { self.skipWaiting(); });
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(
    ks.filter(k => k !== CACHE && k !== SNAPSHOT).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  // 移动端快照：网络优先，失败回落缓存（离线可看上次数据）
  if (url.pathname.endsWith('/console/m')) {
    e.respondWith(fetch(req).then(res => {
      const copy = res.clone();
      caches.open(SNAPSHOT).then(c => c.put(req, copy));
      return res;
    }).catch(() => caches.open(SNAPSHOT).then(c => c.match(req))));
    return;
  }
  const cacheable = /[.](svg|png|webmanifest|css|js)$/.test(url.pathname) ||
                    url.pathname.endsWith('/icon.svg');
  if (!cacheable) return;              // 其它文档/接口一律走网络（避免陈旧看板）
  e.respondWith(caches.open(CACHE).then(c => c.match(req).then(hit =>
    hit || fetch(req).then(res => { c.put(req, res.clone()); return res; }))));
});
"""
    return Response(js, media_type="application/javascript")


@router.get("/embed/token")
async def embed_token(request: Request, panel: str = Query(...),
                      hours: int = Query(720)):
    """签发嵌入令牌（控制台内调用；只读面板，供交付/白标嵌入）"""
    from ..engine.embed import EmbedError, mint
    user = getattr(request.state, "user", None)
    if os.environ.get("INSFLOW_SAAS", "") == "1" and not user:
        return JSONResponse({"ok": False, "error": "需要登录"}, status_code=401)
    # 租户隔离：以登录用户所属工作区签发，绝不回落到「第一个工作区」
    workspace_id = (user or {}).get("workspace_id") or await _default_workspace()
    if user and (user.get("workspace_id") or "") != workspace_id:
        return JSONResponse({"ok": False, "error": "工作区不匹配"}, status_code=403)
    if not panel or ":" not in panel:
        return JSONResponse({"ok": False, "error": "panel 形如 cockpit:traffic"},
                            status_code=400)
    try:
        token = mint(workspace_id, panel, hours=hours)
    except EmbedError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    base = os.environ.get("INSFLOW_BASE_PATH", "")
    url = f"{base}/console/embed?token={token}"
    # 反代/CDN（Cloudflare 等）常无视 no-store 缓存 HTML：iframe 由内联脚本
    # 注入带时间戳的 src，确保每次进入都取到最新数据（令牌本身不变）。
    eid = f"ifEmbed{token[-6:]}"
    snippet = (
        f'<iframe id="{eid}" width="100%" height="420" style="border:0" '
        f'title="Insight Flow 面板" loading="lazy"></iframe>'
        f'<script>(function(){{var f=document.getElementById("{eid}");'
        f'f.src="{url}"+(f.src.indexOf("?")<0?"?":"&")+"_t="+Date.now();}})();</script>')
    return JSONResponse({"ok": True, "token": token, "url": url, "iframe": snippet})


@router.get("/embed", response_class=HTMLResponse, include_in_schema=False)
async def embed_view(request: Request, token: str = Query("")):
    """嵌入视图（只读、无控制台导航；HMAC 令牌鉴权，fail-closed）"""
    from ..engine.embed import EmbedError, verify
    try:
        payload = verify(token)
    except EmbedError as e:
        return HTMLResponse(f'<div style="font:14px/1.6 system-ui;padding:24px">'
                            f'嵌入不可用：{e}</div>', status_code=403)
    workspace_id = payload["w"]
    panel = payload.get("p", "")
    branding = payload.get("b") or {}
    store = await get_store()
    if not await store.get_workspace(workspace_id):
        return HTMLResponse('<div style="padding:24px">工作区不存在</div>', status_code=404)

    kind, _, name = panel.partition(":")
    data, metric, days, panels = None, "", 30, []
    if kind == "metric":
        series = await store.metric_series(workspace_id, name, days=days, limit=120)
        metric = name
        data = {"labels": [p["bucket"] for p in series],
                "y_values": [p["value"] for p in series],
                "rows": [{"bucket": p["bucket"], "value": p["value"]} for p in series],
                "total": sum(p["value"] for p in series)}
        panels = build_board_panels(name, data)
    elif kind in ("cockpit", "board"):
        from .cockpit import COCKPITS
        fn = COCKPITS.get(name)
        if fn:
            data = _embed_normalize(await fn(workspace_id))
            panels = build_board_panels(name, data)
            if kind == "cockpit" and panels:
                panels = panels[:2]        # 单面板嵌入：给最相关的 1-2 块
    return templates.TemplateResponse(request, "embed.html", {
        "request": request, "version": __version__, "workspace_id": workspace_id,
        "panel": panel, "kind": kind, "name": name, "data": data, "metric": metric,
        "branding": branding, "panels": panels,
        "theme": (request.query_params.get("theme") or "").lower(),
    })


@router.get("/explore", response_class=HTMLResponse)
async def explore_page(request: Request, workspace_id: str = Query(""),
                       metric: str = Query(""), entity: str = Query(""),
                       days: float = Query(30), agg: str = Query("sum"),
                       chart_type: str = Query("line", alias="chart"),
                       dim: str = Query("province"), dim2: str = Query("channel"),
                       calc: str = Query("none"), window: int = Query(7),
                       granularity: str = Query("auto")):
    """即席探索：指标 × 维度透视 × 图表类型 × 聚合（URL 可分享）

    能力面：趋势/条形/箱线/散点/网格地图切换、二维透视、维度变量（var.*）、
    只读 SQL 沙箱、口径（语义层）登记。
    """
    if not workspace_id:
        workspace_id = await _default_workspace()
    store = await get_store()
    catalog = await store.metric_catalog(workspace_id, days=180)
    chart = None
    pivot = None
    dim_values: list[tuple[str, float]] = []
    box_groups: list[tuple[str, list[float]]] = []
    scatter_points: list[tuple[float, float, str]] = []
    waterfall_items: list[tuple[str, float]] = []
    candlesticks: list[tuple[str, float, float, float, float]] = []
    candlestick_true = False
    entity_options: list[str] = []
    vars_ = template_vars(request)
    if metric:
        for item in catalog:
            if item["metric"] == metric:
                entity_options = item["entities"]
                break
        df = dict(vars_) or None
        ws_pol = await store.get_workspace(workspace_id)
        _pol_settings = (ws_pol.settings_json if ws_pol else {}) or {}
        policy = _policy_ctx(request, _pol_settings)
        allow = _role_ctx(request, _pol_settings)[1]
        from ..engine.semantics import SemanticError, calc_series, resolve_metric
        defs_map = {d["name"]: d for d in await store.list_metric_defs(workspace_id)}
        calc_used, calc_error = "none", ""
        if metric in defs_map and (defs_map[metric].get("expr") or "").strip():
            # 派生指标：按口径表达式解析（血缘可追溯）
            try:
                resolved = await resolve_metric(workspace_id, metric, days=days, agg=agg)
                series = [{"bucket": p["bucket"], "value": p["value"], "n": 1}
                          for p in resolved["series"]]
            except SemanticError as e:
                calc_error = str(e)
                series = []
        else:
            series = await store.metric_series(workspace_id, metric, days=days, agg=agg,
                                               entity_id=entity or None, limit=200,
                                               dim_filters=df if df else None,
                                               entity_allow=allow or None,
                                               policy=policy)
        if calc and calc != "none" and series:
            from ..engine.semantics import table_calc
            raw = [p["value"] for p in series]
            try:
                table_calc(raw, calc, window=window)   # 校验
                transformed = table_calc(raw, calc, window=window)
                series = [{"bucket": p["bucket"], "value": v, "n": p["n"]}
                          for p, v in zip(series, transformed)]
                calc_used = calc
            except SemanticError as e:
                calc_error = str(e)
        rows = [{"bucket": p["bucket"], "value": p["value"], "n": p["n"]} for p in series]
        chart = {"metric": metric, "entity": entity, "agg": agg, "days": days,
                 "labels": [p["bucket"] for p in series],
                 "y_values": [p["value"] for p in series],
                 "rows": rows, "total": sum(p["value"] for p in series),
                 "type": chart_type, "calc": calc_used,
                 "is_derived": metric in defs_map and bool(
                     (defs_map[metric].get("expr") or "").strip()),
                 "lineage": None}
        if chart["is_derived"]:
            from ..engine.semantics import lineage as _lin
            chart["lineage"] = await _lin(workspace_id, metric)
        dim_rows = await store.metric_dim_breakdown(workspace_id, metric, dim,
                                                    days=days, limit=24)
        dim_values = [(str(r["key"]), float(r["value"])) for r in dim_rows]
        if dim_values:
            pivot = await store.pivot(workspace_id, metric, dim, dim2, days=days,
                                      limit=12)
        # 箱线：每个维度取该维度下的日粒度序列作为样本（真实分布，不是单点）
        if chart_type == "box":
            for key, _ in dim_values[:8]:
                rows_k = await store.metric_series(workspace_id, metric, days=days,
                                                   dim_filters={dim: key}, limit=200)
                if rows_k:
                    box_groups.append((key, [r["value"] for r in rows_k]))
        # 散点：值 × 排名；实体数很多时用全量实体聚合（可触发 GPU 渲染路径）
        if chart_type == "scatter":
            breakdown = await store.metric_breakdown(workspace_id, metric, days=days,
                                                    limit=20000)
            if len(breakdown) >= 40:
                scatter_points = [(float(r["value"]), float(i + 1), str(r["entity_id"]))
                                  for i, r in enumerate(
                                      sorted(breakdown, key=lambda x: -x["value"]))]
            else:
                scatter_points = [(float(v), float(i + 1), str(k))
                                  for i, (k, v) in enumerate(dim_values[:40])]
        # 瀑布：相邻维度差值归因（维度按值降序 → 差值）
        if chart_type == "waterfall":
            prev = 0.0
            for k, v in dim_values[:10]:
                waterfall_items.append((k, float(v) - prev))
                prev = float(v)
        # K 线：真实 OHLC（桶内首/高/低/末，来自逐条观测；无观测则退化为单点）
        if chart_type == "candlestick":
            ohlc = await store.metric_ohlc(workspace_id, metric, days=days,
                                           bucket="day", entity_id=entity or None,
                                           dim_filters=df if df else None)
            for r in ohlc:
                candlesticks.append((str(r["bucket"]), float(r["open"]),
                                     float(r["high"]), float(r["low"]),
                                     float(r["close"])))
            candlestick_true = bool(ohlc) and any(r["n"] > 1 for r in ohlc)
    return templates.TemplateResponse(request, "explore.html", _ctx(
        request, "explore", workspace_id,
        catalog=catalog[:60], chart=chart, pivot=pivot, metric=metric, entity=entity,
        days=days, agg=agg, entity_options=entity_options, chart_type=chart_type,
        dim=dim, dim2=dim2, dim_values=dim_values, vars_=vars_,
        box_groups=box_groups, scatter_points=scatter_points,
        waterfall_items=waterfall_items, candlesticks=candlesticks,
        candlestick_true=candlestick_true,
        dim_keys=await store.dim_keys(workspace_id, metric),
        defs=await store.list_metric_defs(workspace_id),
        calc=calc, window=window, calc_error=calc_error, lineage=chart.get("lineage")
        if chart else None,
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
    billing = BillingManager(workspace_id)
    summary = await billing.usage_summary()
    trial = await billing.trial_status()
    return templates.TemplateResponse(request, "usage.html", _ctx(
        request, "usage", workspace_id,
        plan_id=summary["plan_id"],
        plan_name=summary["plan_name"],
        price=summary["price_usd_month"],
        features=summary["features"],
        sources_allowed=summary["sources_allowed"],
        usage=summary["usage"],
        checked_at=summary["checked_at"],
        all_plans=PLANS, trial=trial,
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
    from ..engine import sso
    return templates.TemplateResponse(request, "login.html", {
        "request": request, "version": __version__, "error": error, "next": next,
        "saas": _SAAS_MODE(), "sso_enabled": sso.enabled(),
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
