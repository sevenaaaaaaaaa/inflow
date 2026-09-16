"""测试计费与配额产品化（套餐/用量/超量告警）"""

import pytest

import insflow.core.files as files_mod
from insflow.core.entities import Workspace
from insflow.core.store import Store, reset_store
from insflow.engine.billing import PLANS, BillingManager


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)

    s = Store(db_path=tmp_path / "test.db")
    await s.connect()
    await s.migrate()
    reset_store(s)
    await s.create_workspace(Workspace(id="test-ws", name="Test WS"))

    yield {"store": s}

    reset_store(None)
    await s.close()


class TestPlans:
    def test_four_tiers_aligned_with_prd(self):
        """套餐与 PRD 定价表 / 数据源矩阵对齐"""
        assert set(PLANS) == {"free", "growth", "scale", "enterprise"}
        assert PLANS["free"]["limits"]["competitors"] == 0
        assert PLANS["growth"]["limits"]["competitors"] == 3
        assert PLANS["scale"]["limits"]["white_label"] is True
        # L1 免费底座数据源
        assert {"gsc", "ga4", "crux"} <= set(PLANS["free"]["sources"])
        # L2 增加付费引擎
        assert "dataforseo-labs" in PLANS["growth"]["sources"]
        assert "dataforseo-ads" in PLANS["scale"]["sources"]
        # Enterprise 不限量
        assert PLANS["enterprise"]["limits"]["monthly_api_calls"] is None

    async def test_default_plan_is_free(self, env):
        mgr = BillingManager("test-ws")
        plan = await mgr.get_plan()
        assert plan["plan_id"] == "free"

    async def test_set_plan(self, env):
        mgr = BillingManager("test-ws")
        await mgr.set_plan("growth")
        assert (await mgr.get_plan())["plan_id"] == "growth"

    async def test_set_unknown_plan_rejected(self, env):
        mgr = BillingManager("test-ws")
        with pytest.raises(Exception):
            await mgr.set_plan("ultra")


class TestQuota:
    async def test_usage_visible(self, env):
        mgr = BillingManager("test-ws")
        mgr.record_usage("api_calls", 500)
        mgr.record_usage("agent_asks", 12)

        summary = await mgr.usage_summary()
        u = summary["usage"]
        assert u["api_calls"]["used"] == 500
        assert u["agent_asks"]["limit"] == 0  # free 档无 Agent
        assert u["api_calls"]["limit"] == PLANS["free"]["limits"]["monthly_api_calls"]

    async def test_check_quota_pass_and_warn(self, env):
        mgr = BillingManager("test-ws")
        mgr.record_usage("api_calls", 1900)  # free 限额 2000，1900+150=2050 超限
        result = await mgr.check_quota("api_calls", requested=150)
        assert result["allowed"] is False

    async def test_warning_at_80pct(self, env):
        mgr = BillingManager("test-ws")
        mgr.record_usage("api_calls", 1700)  # 85% → warning 事件
        result = await mgr.check_quota("api_calls", requested=10)
        assert result["allowed"] is True
        assert result["ratio"] >= 0.8

    async def test_unlimited_for_enterprise(self, env):
        await BillingManager("test-ws").set_plan("enterprise")
        mgr = BillingManager("test-ws")
        mgr.record_usage("api_calls", 10**9)
        result = await mgr.check_quota("api_calls", requested=10**9)
        assert result["allowed"] is True

    async def test_cost_quota(self, env):
        mgr = BillingManager("test-ws")
        mgr.record_usage("cost_usd", 4.9)  # free 限额 $5
        result = await mgr.check_quota("cost_usd", requested=0.5)
        assert result["allowed"] is False


class TestTrial:
    def test_trial_constants(self):
        from insflow.engine.billing import TRIAL_DAYS, TRIAL_PLAN
        assert TRIAL_DAYS == 14
        assert TRIAL_PLAN == "growth"

    async def test_start_trial_and_resolve(self, env):
        from insflow.engine.billing import BillingManager
        mgr = BillingManager("test-ws")
        assert await mgr.get_plan_id() == "free"

        result = await mgr.start_trial()
        assert result["ok"] is True
        # 试用期内解析为 Growth
        assert await mgr.get_plan_id() == "growth"
        status = await mgr.trial_status()
        assert status["active"] is True
        assert status["days_left"] >= 13

    async def test_trial_only_once(self, env):
        from insflow.engine.billing import BillingManager
        mgr = BillingManager("test-ws")
        await mgr.start_trial()
        with pytest.raises(Exception):
            await mgr.start_trial()

    async def test_expired_trial_falls_back(self, env):
        from datetime import datetime, timedelta, timezone
        from insflow.core.store import get_store
        from insflow.engine.billing import BillingManager

        mgr = BillingManager("test-ws")
        await mgr.start_trial()
        # 手动把试用期改到过去
        store = await get_store()
        ws = await store.get_workspace("test-ws")
        ws.settings_json["trial_until"] = (
            datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        await store.update_workspace(ws)

        assert await mgr.get_plan_id() == "free"
        assert (await mgr.trial_status())["active"] is False
