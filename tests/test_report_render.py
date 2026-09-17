"""测试报告可视化渲染（P1：MD → 可视 HTML）"""

import pytest

import insflow.core.files as files_mod
from insflow.core.entities import Insight, Workspace
from insflow.core.files import ReportStore
from insflow.core.store import Store, get_store, reset_store
from insflow.engine.report_render import CHART_BLOCKS, ReportRenderer


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)
    monkeypatch.setenv("INSFLOW_MASTER_KEY", "mk")
    s = Store(db_path=tmp_path / "test.db")
    await s.connect()
    await s.migrate()
    reset_store(s)
    await s.create_workspace(Workspace(id="test-ws", name="T"))
    # 造一份周报 + 一条负面预警（供图表块取数）
    ReportStore("test-ws").save_report("weekly", "# 增长周报\n\n## 摘要\n\n本周共 3 条洞察。")
    await s.create_insight(Insight(
        workspace_id="test-ws", type="topic_negative_alert",
        title="舆情负面预警：某品牌", summary="负向占比 45%",
        severity="high", confidence=0.8,
        evidence_json=[{"type": "negative_sentiment", "negative_count": 9,
                        "negative_ratio": 0.45,
                        "examples": [{"text": "质量差", "hits": ["差"]}]}],
    ))
    yield {"store": s}
    reset_store(None)
    await s.close()


class TestReportRenderer:
    async def test_render_injects_charts(self, env):
        store = ReportStore("test-ws")
        filename = store.list_reports("weekly")[0].name
        html = await ReportRenderer("test-ws").render("weekly", filename)

        assert html.startswith("<!DOCTYPE html>")
        assert "<svg" in html                       # 内联 SVG 图表
        assert "增长周报" in html                    # 原文保留
        assert "情报总览" in html and "舆情情绪" in html  # 图表块标题
        assert "打印 / 导出 PDF" in html

    async def test_self_contained_tokens(self, env):
        """自包含：内联设计令牌（脱离控制台可渲染）"""
        store = ReportStore("test-ws")
        html = await ReportRenderer("test-ws").render(
            "weekly", store.list_reports("weekly")[0].name)
        assert "--accent" in html and "<style>" in html
        assert "http://" not in html.split("<style>")[0]  # 无外部 CSS 依赖

    async def test_missing_report_raises(self, env):
        with pytest.raises(FileNotFoundError):
            await ReportRenderer("test-ws").render("weekly", "ghost.md")

    async def test_export_writes_file(self, env):
        store = ReportStore("test-ws")
        filename = store.list_reports("weekly")[0].name
        result = await ReportRenderer("test-ws").export_html("weekly", filename)
        assert result["ok"] is True
        from pathlib import Path
        assert Path(result["path"]).exists()

    async def test_branding_applied(self, env):
        store = ReportStore("test-ws")
        html = await ReportRenderer("test-ws").render(
            "weekly", store.list_reports("weekly")[0].name,
            branding={"company": "代理商 A", "client": "客户甲", "accent_color": "#ff6600"})
        assert "Prepared by 代理商 A" in html
        assert "客户甲" in html
        assert "#ff6600" in html

    async def test_no_charts_category_safe(self, env):
        """无图表块类别（invoices）也应正常渲染"""
        store = ReportStore("test-ws")
        store.save_report("invoices", "# 对账单\n内容")
        html = await ReportRenderer("test-ws").render(
            "invoices", store.list_reports("invoices")[0].name)
        assert "对账单" in html

    async def test_chart_blocks_mapping(self):
        assert "overview" in CHART_BLOCKS["weekly"]
        assert "action_loop" in CHART_BLOCKS["verification"]
        assert len(CHART_BLOCKS["deep-dive"]) >= 3
