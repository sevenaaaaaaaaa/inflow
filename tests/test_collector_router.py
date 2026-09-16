"""测试监控 collector 路由（keyword / brand_mention / journey）"""

import json

import pytest

import insflow.core.files as files_mod
from insflow.core.entities import Workspace
from insflow.core.scheduler import Scheduler
from insflow.core.store import Store, get_store, reset_store
from insflow.engine.monitors import MonitorService


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)
    monkeypatch.setenv("INSFLOW_MASTER_KEY", "mk")

    s = Store(db_path=tmp_path / "test.db")
    await s.connect()
    await s.migrate()
    reset_store(s)
    await s.create_workspace(Workspace(id="test-ws", name="Test WS"))

    # 保险库预置 OAuth 凭据（向导产出）
    from insflow.core.security import get_vault
    vault = get_vault()
    vault.set("gsc_oauth_tokens", json.dumps({"access_token": "gsc-tok"}))
    vault.set("ga4_oauth_tokens", json.dumps({"access_token": "ga4-tok"}))

    svc = MonitorService("test-ws", scheduler=Scheduler("test-ws"))
    yield {"store": s, "service": svc}

    reset_store(None)
    await s.close()


class TestKeywordCollector:
    async def test_run_creates_opportunity_insight(self, env, monkeypatch):
        """GSC fake 数据 → 高曝光低 CTR 词 → 机会洞察"""
        svc = env["service"]
        m = await svc.create("keyword", {"site": "sc-domain:test.com"})

        class FakeResp:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {"rows": [
                    {"keys": ["增长自动化平台"], "clicks": 5, "impressions": 900,
                     "ctr": 0.02, "position": 8.0},
                    {"keys": ["正常词"], "clicks": 60, "impressions": 500,
                     "ctr": 0.12, "position": 3.0},
                ]}

        class FakeAsyncClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return None
            async def post(self, *a, **kw): return FakeResp()

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: FakeAsyncClient())

        result = await svc.run(m["id"])
        assert result["kind"] == "keyword"
        assert result["queries"] == 2
        assert result["insights_created"] == 1

        store = await get_store()
        insights = await store.list_insights("test-ws")
        assert insights[0].type == "keyword_opportunity"
        assert "增长自动化" in insights[0].summary

    async def test_requires_auth(self, env, monkeypatch):
        from insflow.core.security import get_vault
        get_vault().delete("gsc_oauth_tokens")

        svc = env["service"]
        m = await svc.create("keyword", {"site": "sc-domain:x.com"})
        result = await svc.run(m["id"])
        assert "error" in result
        assert "未授权" in result["error"]


class TestBrandMention:
    async def test_run_with_gdelt(self, env, monkeypatch):
        svc = env["service"]
        m = await svc.create("brand_mention", {"query": "competitor X"})

        class FakeResp:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {"articles": [
                    {"title": f"t{i}", "domain": f"d{i%3}.com", "seendate": "20260916"}
                    for i in range(30)
                ]}

        class FakeAsyncClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return None
            async def get(self, *a, **kw): return FakeResp()

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: FakeAsyncClient())

        result = await svc.run(m["id"])
        assert result["mentions"] == 30
        assert result["insights_created"] == 1

        store = await get_store()
        insights = await store.list_insights("test-ws")
        assert insights[0].type == "brand_mention_spike"

    async def test_low_mentions_no_insight(self, env, monkeypatch):
        svc = env["service"]
        m = await svc.create("brand_mention", {"query": "quiet"})

        class FakeResp:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {"articles": [{"title": "x", "domain": "d.com"} for _ in range(5)]}

        class FakeAsyncClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return None
            async def get(self, *a, **kw): return FakeResp()

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: FakeAsyncClient())

        result = await svc.run(m["id"])
        assert result["insights_created"] == 0


class TestJourneyCollector:
    async def test_run_detects_gap(self, env, monkeypatch):
        """GA4 漏斗事件 → 步数骤降 → journey_gap 洞察"""
        svc = env["service"]
        m = await svc.create("journey", {
            "property_id": "123",
            "steps": [
                {"name": "visit_pricing", "event": "page_view_pricing"},
                {"name": "checkout_started", "event": "begin_checkout"},
            ],
        })

        class FakeResp:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {
                    "dimensionHeaders": [{"name": "eventName"}],
                    "metricHeaders": [{"name": "eventCount"}],
                    "rows": [
                        {"dimensionValues": [{"value": "page_view_pricing"}],
                         "metricValues": [{"value": "1000"}]},
                        {"dimensionValues": [{"value": "begin_checkout"}],
                         "metricValues": [{"value": "180"}]},
                    ],
                }

        class FakeAsyncClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return None
            async def post(self, *a, **kw): return FakeResp()

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: FakeAsyncClient())

        result = await svc.run(m["id"])
        assert result["steps_collected"] == 2
        assert result["insights_created"] == 1

        store = await get_store()
        insights = await store.list_insights("test-ws")
        assert insights[0].type == "journey_gap"

    async def test_missing_steps_rejected(self, env):
        svc = env["service"]
        m = await svc.create("journey", {"property_id": "123"})
        result = await svc.run(m["id"])
        assert "error" in result
        assert "steps" in result["error"]
