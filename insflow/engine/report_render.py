"""Insight Flow 报告可视化渲染（P1：MD → 可视 HTML）

把 Markdown 报告升级为**自包含可视 HTML**：
- 保留原文（Markdown → HTML）
- 在正文前注入该报告类型的**图表块**（来自驾驶舱聚合，复用 viz SVG）
- 内联设计令牌，脱离控制台也能正确渲染；可打印导出 PDF；支持白标

图表块映射（按报告类别）：
- weekly / diagnosis → 概览 KPI + 情绪趋势 + 风险雷达
- competitors      → 关键词缺口 + 竞品异动时间线
- verification     → 验证结论构成 + 模型命中率
- deep-dive        → 概览 + 流量 + 舆情 + 行动（顾问交付物）
"""

from datetime import datetime, timezone

from ..core.files import ReportStore
from ..viz import charts as viz
from ..viz.base import wrap_html

# 报告类别 → 图表块清单
CHART_BLOCKS = {
    "weekly": ["overview", "sentiment"],
    "diagnosis": ["overview", "traffic"],
    "competitors": ["competitor"],
    "verification": ["action_loop"],
    "deep-dive": ["overview", "traffic", "sentiment", "action_loop"],
    "maturity": ["overview"],
    "invoices": [],
}


class ReportRenderer:
    """报告 → 可视 HTML"""

    def __init__(self, workspace_id: str):
        self.workspace_id = workspace_id

    async def render(self, category: str, filename: str, *,
                     branding: dict | None = None) -> str:
        """读取 Markdown 报告 → 注入图表 → 返回自包含 HTML"""
        store = ReportStore(self.workspace_id)
        markdown = store.get_report(category, filename)
        if markdown is None:
            raise FileNotFoundError(f"报告不存在: {category}/{filename}")

        chart_html = await self._charts(category)
        body = chart_html + self._md_to_html(markdown)
        title = f"{filename.replace('.md', '')} · {category}"
        return wrap_html(title, body, branding=branding)

    async def export_html(self, category: str, filename: str, *,
                          branding: dict | None = None) -> dict:
        """渲染并落盘 data/reports/{ws}/visual/"""
        html = await self.render(category, filename, branding=branding)
        out = ReportStore(self.workspace_id).base / "visual" / f"{filename.replace('.md', '')}.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        return {"ok": True, "path": str(out)}

    # ========== 图表块 ==========

    async def _charts(self, category: str) -> str:
        from ..web.cockpit import COCKPITS
        blocks = CHART_BLOCKS.get(category, [])
        if not blocks:
            return ""
        parts = []
        for name in blocks:
            fn = COCKPITS.get(name)
            if not fn:
                continue
            try:
                data = await fn(self.workspace_id)
            except Exception:
                continue
            rendered = self._render_block(name, data)
            if rendered:
                parts.append(rendered)
        return "".join(parts)

    def _render_block(self, name: str, data: dict) -> str:
        if name == "overview":
            k = data["kpis"]
            cards = "".join([
                viz.kpi_card("本周期洞察", k["recent"]),
                viz.kpi_card("负面预警", k["alerts"], color="var(--danger)"),
                viz.kpi_card("已验证闭环", k["verified"], color="var(--ok)"),
                viz.kpi_card("采集成功/失败", f"{k['collect_ok']} / {k['collect_fail']}",
                             color="var(--ok)" if not k["collect_fail"] else "var(--warn)"),
            ])
            risk = viz.bar_chart(data["risk"], threshold=0.35, threshold_label="预警线 35%")
            return (f'<h2>情报总览</h2><div class="cards">{cards}</div>'
                    f'<div class="card"><div class="sub" style="margin-bottom:6px">'
                    f'各监测主体负向占比</div>{risk}</div>')

        if name == "sentiment":
            t = data["trend"]
            chart = (viz.line_chart([
                {"name": "负向占比", "values": t["negative_ratio"], "color": "var(--danger)"},
                {"name": "情绪均分", "values": t["score"], "color": "var(--ok)"},
            ], t["labels"]) if t["labels"] else viz.placeholder(720, 150, "暂无舆情趋势"))
            channels = viz.percent_bar(data["channels"]) if data["channels"] else ""
            return (f'<h2>舆情情绪</h2><div class="card">{chart}</div>'
                    + (f'<div class="card">{channels}</div>' if channels else ""))

        if name == "traffic":
            t = data["trend"]
            chart = (viz.line_chart([
                {"name": "GSC 点击", "values": t["clicks"]},
                {"name": "GA4 会话", "values": t["sessions"]},
            ], t["labels"]) if t["labels"] else viz.placeholder(720, 150, "暂无流量趋势"))
            scatter = (viz.scatter(data["scatter"], x_label="曝光量", y_label="平均排名")
                       if data["scatter"] else "")
            return (f'<h2>流量趋势</h2><div class="card">{chart}</div>'
                    + (f'<div class="card">{scatter}</div>' if scatter else ""))

        if name == "competitor":
            gaps = (viz.bar_chart(data["gaps"]) if data["gaps"]
                    else viz.placeholder(720, 100, "暂无关键词缺口"))
            timeline = viz.timeline(data["timeline"]) if data["timeline"] else ""
            return (f'<h2>竞品情报</h2><div class="card">{gaps}</div>'
                    + (f'<div class="card">{timeline}</div>' if timeline else ""))

        if name == "action_loop":
            k, v = data["kpis"], data["verdicts"]
            cards = "".join([
                viz.kpi_card("动作总数", k["total"]),
                viz.kpi_card("已验证", k["verified"], color="var(--ok)"),
                viz.kpi_card("待验证", k["verifying"], color="var(--warn)"),
                viz.kpi_card("重试耗尽", k["dead"], color="var(--danger)"),
            ])
            verdict = viz.percent_bar([
                ("有效", v.get("effective", 0), "var(--ok)"),
                ("中性", v.get("neutral", 0), "var(--muted)"),
                ("有害", v.get("harmful", 0), "var(--danger)"),
            ])
            hit = (viz.bar_chart([(m["model_id"], m["hit_rate"]) for m in data["effectiveness"]],
                                 value_fmt=viz.fmt_pct)
                   if data["effectiveness"] else viz.placeholder(720, 100, "暂无模型命中率"))
            return (f'<h2>行动与验证</h2><div class="cards">{cards}</div>'
                    f'<div class="card">{verdict}</div>'
                    f'<div class="card"><div class="sub" style="margin-bottom:6px">'
                    f'模型命中率</div>{hit}</div>')
        return ""

    # ========== Markdown → HTML（复用白标渲染器）==========

    def _md_to_html(self, md: str) -> str:
        from .white_label import WhiteLabelRenderer
        return WhiteLabelRenderer(self.workspace_id)._md_to_html(md)
