"""测试全域主题监测（S3：多渠道聚合，面向超级个体）"""

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
    await s.create_workspace(Workspace(id="test-ws", name="T"))
    svc = MonitorService("test-ws", scheduler=Scheduler("test-ws"))
    yield {"service": svc, "store": s}
    reset_store(None)
    await s.close()


def _fake_http(items_by_url):
    """构造按 URL 分派的假 httpx.AsyncClient"""
    class FakeResp:
        def __init__(self, payload): self._p = payload
        def raise_for_status(self): pass
        def json(self): return self._p

    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url, params=None, headers=None, timeout=None):
            for key, payload in items_by_url.items():
                if key in url:
                    return FakeResp(payload)
            return FakeResp({})
        async def post(self, url, json=None, headers=None, timeout=None):
            return FakeResp({"organic": [
                {"title": "搜索命中 1", "domain": "s1.com", "link": "https://s1.com", "snippet": "x"},
                {"title": "搜索命中 2", "domain": "s2.com", "link": "https://s2.com", "snippet": "y"},
            ]})
    return FakeClient


class TestTopicMonitor:
    async def test_multichannel_aggregation(self, env, monkeypatch):
        """news + search 聚合 → 指标入库 + 舆情概览洞察"""
        svc = env["service"]
        m = await svc.create("topic", {
            "query": "超级个体 增长", "channels": ["news", "search"],
            "min_mentions": 3,
        })

        import httpx
        fake = _fake_http({"gdeltproject": {"articles": [
            {"title": f"新闻 {i}", "domain": f"n{i}.com", "url": f"https://n{i}.com"}
            for i in range(5)
        ]}})
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: fake())
        # search 渠道需要凭据
        from insflow.core.security import get_vault
        get_vault().set("serper_api_key", "fake-key")

        result = await svc.run(m["id"])
        assert result["kind"] == "topic"
        assert result["channels"]["news"] == 5
        assert result["channels"]["search"] == 2
        assert result["total_mentions"] == 7
        assert result["insights_created"] == 1

        store = await get_store()
        insights = await store.list_insights("test-ws")
        assert insights[0].type == "topic_digest"
        assert "超级个体" in insights[0].title

    async def test_channel_failure_isolated(self, env, monkeypatch):
        """单渠道失败不影响整体（容错）"""
        svc = env["service"]
        m = await svc.create("topic", {"query": "x", "channels": ["news", "search"],
                                       "min_mentions": 99})

        import httpx

        class BoomClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return None
            async def get(self, *a, **kw): raise RuntimeError("gdelt 429")
            async def post(self, *a, **kw): raise RuntimeError("serper down")

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: BoomClient())
        result = await svc.run(m["id"])
        assert result["total_mentions"] == 0
        assert "news" in result["errors"]
        assert "search" in result["errors"]

    async def test_no_website_required(self, env, monkeypatch):
        """主题监测不要求自有网站（面向超级个体）"""
        svc = env["service"]
        # 只用 query，无 site/url
        m = await svc.create("topic", {"query": "某个话题", "channels": ["news"],
                                       "min_mentions": 2})
        import httpx
        fake = _fake_http({"gdeltproject": {"articles": [
            {"title": "a", "domain": "d.com"}, {"title": "b", "domain": "d.com"},
            {"title": "c", "domain": "d.com"},
        ]}})
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: fake())
        result = await svc.run(m["id"])
        assert result["insights_created"] == 1

    async def test_topic_kind_registered(self, env):
        from insflow.engine.monitors import MonitorService as MS
        assert "topic" in MS.KINDS
