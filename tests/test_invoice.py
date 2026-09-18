"""测试计费对账单（R3-3）"""

import pytest

import insflow.core.files as files_mod
from insflow.core.entities import Workspace
from insflow.engine.billing import PLANS
from insflow.engine.invoice import InvoiceBuilder


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)
    monkeypatch.setenv("INSFLOW_MASTER_KEY", "mk")
    from insflow.core.store import Store, reset_store

    s = Store(db_path=tmp_path / "test.db")
    await s.connect()
    await s.migrate()
    reset_store(s)
    await s.create_workspace(Workspace(id="test-ws", name="Test WS"))
    yield {"store": s}
    reset_store(None)
    await s.close()


class TestInvoice:
    async def test_build_free_plan(self, env):
        result = await InvoiceBuilder("test-ws").build(month="2026-09")
        assert result["invoice_path"]
        md = result["markdown"]
        assert "对账单 · 2026-09" in md
        assert f"${PLANS['free']['price_usd_month']}" in md

    async def test_usage_records_included(self, env):
        result = await InvoiceBuilder("test-ws").build(
            usage_records={"api_calls": 1200, "agent_asks": 3, "cost_usd": 4.2})
        md = result["markdown"]
        assert "1200" in md
        assert "$4.20" in md

    async def test_scale_plan_report_period(self, env, tmp_path):
        await (env["store"]).get_workspace("test-ws")
        from insflow.core.store import get_store
        ws = await (await get_store()).get_workspace("test-ws")
        ws.settings_json = {"plan": "enterprise"}
        await (await get_store()).update_workspace(ws)
        result = await InvoiceBuilder("test-ws").build()
        assert "报价制" in result["markdown"]
