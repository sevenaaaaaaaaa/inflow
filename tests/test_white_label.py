"""测试白标报告（品牌注入/套餐门控/导出）"""

import pytest

import insflow.core.files as files_mod
from insflow.core.entities import Workspace
from insflow.core.store import Store, reset_store
from insflow.engine.billing import BillingManager
from insflow.engine.white_label import WhiteLabelConfig, WhiteLabelRenderer


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)

    s = Store(db_path=tmp_path / "test.db")
    await s.connect()
    await s.migrate()
    reset_store(s)
    await s.create_workspace(Workspace(id="test-ws", name="Test WS"))

    # 造一份诊断报告
    from insflow.core.files import ReportStore
    ReportStore("test-ws").save_report("diagnosis", "# 流量诊断\n\n| 项 | 值 |\n|---|---|\n| 洞察 | 3 条 |\n\n- 建议 A\n- 建议 B")

    yield {"store": s}

    reset_store(None)
    await s.close()


class TestRendering:
    async def test_render_html_contains_brand(self, env):
        renderer = WhiteLabelRenderer("test-ws", WhiteLabelConfig(
            company="代理商 A", logo_url="https://x.com/logo.png",
            accent_color="#ff6600", footer="© 代理公司", disclaimer="仅供内部参考",
        ))
        doc = await renderer.render_html("# 标题", client_name="客户甲")

        assert "Prepared by 代理商 A" in doc
        assert "客户：客户甲" in doc
        assert "#ff6600" in doc
        assert "仅供内部参考" in doc
        assert doc.strip().startswith("<!DOCTYPE html>")

    async def test_html_escapes(self, env):
        renderer = WhiteLabelRenderer("test-ws", WhiteLabelConfig(company="<script>alert(1)</script>"))
        doc = await renderer.render_html("# t")
        assert "<script>alert(1)" not in doc
        assert "&lt;script&gt;" in doc

    async def test_md_tables_rendered(self, env):
        renderer = WhiteLabelRenderer("test-ws")
        doc = await renderer.render_html("| A | B |\n|---|---|\n| 1 | 2 |")
        assert "<table>" in doc
        assert "<th>A</th>" in doc
        assert "<td>2</td>" in doc

    async def test_inline_markdown(self, env):
        renderer = WhiteLabelRenderer("test-ws")
        doc = await renderer.render_html("这是 **重点** 和 `code`")
        assert "<strong>重点</strong>" in doc
        assert "<code>code</code>" in doc


class TestGating:
    async def test_free_plan_blocked(self, env):
        """Free 档无 white_label → 拒绝导出"""
        renderer = WhiteLabelRenderer("test-ws")
        result = await renderer.export("diagnosis", "report.md")
        assert result["ok"] is False
        assert "白标" in result["detail"]

    async def test_scale_plan_allowed(self, env):
        await BillingManager("test-ws").set_plan("scale")
        renderer = WhiteLabelRenderer("test-ws", WhiteLabelConfig(company="代理公司"))

        from insflow.core.files import ReportStore
        reports = ReportStore("test-ws").list_reports("diagnosis")
        filename = reports[0].name  # 诊断报告按 ISO 周命名

        result = await renderer.export("diagnosis", filename, client_name="客户A")
        assert result["ok"] is True
        assert ".html" in result["path"]

    async def test_missing_report(self, env):
        await BillingManager("test-ws").set_plan("scale")
        renderer = WhiteLabelRenderer("test-ws")
        result = await renderer.export("diagnosis", "ghost.md")
        assert result["ok"] is False
