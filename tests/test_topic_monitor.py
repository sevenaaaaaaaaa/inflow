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


class TestNegativeAlert:
    async def test_negative_alert_triggered(self, env, monkeypatch):
        """负向占比超阈值 → topic_negative_alert（high/critical）"""
        svc = env["service"]
        m = await svc.create("topic", {
            "query": "某品牌", "channels": ["news"], "min_mentions": 5,
            "negative_alert_ratio": 0.3, "negative_alert_min": 3,
        })

        import httpx
        negative_articles = [
            {"title": "某品牌产品质量太差，已申请退款", "domain": "n1.com"},
            {"title": "垃圾服务，客服态度糟糕", "domain": "n2.com"},
            {"title": "某品牌涉嫌虚假宣传，用户维权", "domain": "n3.com"},
            {"title": "某品牌发布会顺利举行", "domain": "n4.com"},
            {"title": "某品牌获行业奖项", "domain": "n5.com"},
        ]

        class FakeResp:
            def raise_for_status(self): pass
            def json(self): return {"articles": negative_articles}

        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return None
            async def get(self, *a, **kw): return FakeResp()

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: FakeClient())

        result = await svc.run(m["id"])
        assert result["sentiment"]["counts"]["negative"] >= 3
        assert result["sentiment"]["ratios"]["negative"] >= 0.3

        from insflow.core.store import get_store
        insights = await (await get_store()).list_insights("test-ws")
        types = {i.type for i in insights}
        assert "topic_negative_alert" in types
        alert = next(i for i in insights if i.type == "topic_negative_alert")
        assert alert.severity.value in ("high", "critical")

    async def test_no_alert_below_threshold(self, env, monkeypatch):
        svc = env["service"]
        m = await svc.create("topic", {"query": "x", "channels": ["news"],
                                       "min_mentions": 3, "negative_alert_min": 10})

        import httpx

        class FakeResp:
            def raise_for_status(self): pass
            def json(self): return {"articles": [
                {"title": "不错的产品", "domain": "a.com"},
                {"title": "很好用", "domain": "b.com"},
                {"title": "推荐", "domain": "c.com"},
            ]}

        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return None
            async def get(self, *a, **kw): return FakeResp()

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: FakeClient())
        result = await svc.run(m["id"])
        from insflow.core.store import get_store
        insights = await (await get_store()).list_insights("test-ws")
        assert not any(i.type == "topic_negative_alert" for i in insights)
