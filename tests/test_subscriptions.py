"""测试洞察订阅推送（G-5）"""


import pytest

import insflow.core.files as files_mod
from insflow.core.entities import Insight, Workspace
from insflow.core.store import Store, reset_store
from insflow.engine.subscriptions import (
    SubscriptionError,
    SubscriptionService,
    notify_new_insight,
)


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)
    monkeypatch.setenv("INSFLOW_MASTER_KEY", "mk")

    s = Store(db_path=tmp_path / "test.db")
    await s.connect()
    await s.migrate()
    reset_store(s)
    await s.create_workspace(Workspace(id="test-ws", name="T"))
    yield {"store": s, "svc": SubscriptionService("test-ws")}
    reset_store(None)
    await s.close()


def _insight(**over):
    base = dict(workspace_id="test-ws", type="topic_negative_alert",
                title="舆情负面预警：某品牌", summary="负向占比 45%",
                severity="high", confidence=0.8, evidence_json=[{"type": "t"}])
    base.update(over)
    return Insight(**base)


class TestSubscriptionCRUD:
    async def test_create_and_list(self, env):
        svc = env["svc"]
        sub = await svc.create("飞书告警", ["feishu"],
                               {"feishu_url": "https://open.feishu.cn/open-apis/bot/v2/hook/x"})
        assert sub["id"]
        assert sub["channels"] == ["feishu"]
        assert sub["mode"] == "immediate"
        assert len(await svc.list()) == 1

    async def test_target_validation(self, env):
        with pytest.raises(SubscriptionError):
            await env["svc"].create("缺地址", ["feishu"], {})

    async def test_channel_validation(self, env):
        with pytest.raises(SubscriptionError):
            await env["svc"].create("坏渠道", ["telegram"], {})

    async def test_delete(self, env):
        svc = env["svc"]
        sub = await svc.create("x", ["webhook"], {"webhook_url": "https://h/x"})
        assert await svc.delete(sub["id"]) is True
        assert await svc.list() == []

    async def test_toggle_enabled(self, env):
        svc = env["svc"]
        sub = await svc.create("x", ["webhook"], {"webhook_url": "https://h/x"})
        assert await svc.set_enabled(sub["id"], False)
        assert (await svc.get(sub["id"]))["enabled"] is False


class TestFilterMatch:
    async def test_severity_filter(self, env):
        svc = env["svc"]
        await svc.create("高优", ["webhook"], {"webhook_url": "https://h/x"},
                         filters={"severity": ["critical", "high"]})
        subs = await svc.list()
        from insflow.engine.subscriptions import _matches
        assert _matches(_insight(severity="high"), subs[0]["filters"])
        assert not _matches(_insight(severity="low"), subs[0]["filters"])

    async def test_type_prefix_filter(self, env):
        from insflow.engine.subscriptions import _matches
        assert _matches(_insight(), {"type_prefix": ["topic_"]})
        assert not _matches(_insight(), {"type_prefix": ["competitor_"]})

    async def test_query_and_confidence(self, env):
        from insflow.engine.subscriptions import _matches
        assert _matches(_insight(), {"query": "某品牌"})
        assert not _matches(_insight(), {"query": "不存在的词"})
        assert not _matches(_insight(confidence=0.3), {"min_confidence": 0.6})


class TestDispatch:
    async def test_immediate_push(self, env, monkeypatch):
        """匹配订阅 → 走 Action Router 推送"""
        svc = env["svc"]
        await svc.create("告警群", ["feishu", "webhook"],
                         {"feishu_url": "https://open.feishu.cn/open-apis/bot/v2/hook/x",
                          "webhook_url": "https://n8n.example.com/webhook/y",
                          "webhook_secret": "s"})

        calls = []

        class FakeRouter:
            async def dispatch(self, action, ctx):
                calls.append(action["action_type"])
                return {"ok": True, "ref": "x", "detail": ""}

        import insflow.engine.subscriptions as mod
        monkeypatch.setattr(mod, "get_action_router", lambda: FakeRouter())

        result = await svc.dispatch(_insight())
        assert result["sent"] == 2  # feishu + webhook
        assert set(calls) == {"feishu.notify", "webhook.generic"}

    async def test_filter_blocks_push(self, env, monkeypatch):
        svc = env["svc"]
        await svc.create("只要竞品", ["webhook"], {"webhook_url": "https://h/x"},
                         filters={"type_prefix": ["competitor_"]})
        calls = []

        class FakeRouter:
            async def dispatch(self, action, ctx):
                calls.append(action); return {"ok": True}

        import insflow.engine.subscriptions as mod
        monkeypatch.setattr(mod, "get_action_router", lambda: FakeRouter())

        result = await svc.dispatch(_insight(type="topic_negative_alert"))
        assert result["sent"] == 0
        assert calls == []

    async def test_push_failure_isolated(self, env, monkeypatch):
        """推送失败不影响洞察创建（记录事件）"""
        svc = env["svc"]
        await svc.create("坏地址", ["webhook"], {"webhook_url": "https://h/x"})

        class BoomRouter:
            async def dispatch(self, action, ctx):
                raise RuntimeError("network down")

        import insflow.engine.subscriptions as mod
        monkeypatch.setattr(mod, "get_action_router", lambda: BoomRouter())

        result = await svc.dispatch(_insight())
        assert result["failed"] == 1  # 不抛出

    async def test_disabled_skipped(self, env):
        svc = env["svc"]
        sub = await svc.create("停用", ["webhook"], {"webhook_url": "https://h/x"})
        await svc.set_enabled(sub["id"], False)
        result = await svc.dispatch(_insight())
        assert result["sent"] == 0

    async def test_daily_mode_skips_immediate(self, env):
        svc = env["svc"]
        await svc.create("每日汇总", ["webhook"], {"webhook_url": "https://h/x"}, mode="daily")
        result = await svc.dispatch(_insight())
        assert result["sent"] == 0

    async def test_notify_new_insight_helper(self, env, monkeypatch):
        """洞察创建后助手：查库 + 分发 + 异常吞掉"""
        svc = env["svc"]
        store = env["store"]
        await svc.create("x", ["webhook"], {"webhook_url": "https://h/x"})
        insight = await store.create_insight(_insight())

        class FakeRouter:
            async def dispatch(self, action, ctx):
                return {"ok": True}

        import insflow.engine.subscriptions as mod
        monkeypatch.setattr(mod, "get_action_router", lambda: FakeRouter())

        result = await notify_new_insight("test-ws", insight.id)
        assert result["sent"] == 1

    async def test_notify_missing_insight(self, env):
        assert await notify_new_insight("test-ws", "ghost") == {"sent": 0, "failed": 0}


class TestDailyDispatch:
    async def test_daily_aggregates_24h(self, env, monkeypatch):
        svc = env["svc"]
        store = env["store"]
        await svc.create("每日", ["webhook"], {"webhook_url": "https://h/x"}, mode="daily")

        # 造两条近 24h 洞察 + 一条旧洞察
        await store.create_insight(_insight(title="新洞察1"))
        await store.create_insight(_insight(title="新洞察2"))

        sent = []

        class FakeRouter:
            async def dispatch(self, action, ctx):
                sent.append(action["title"]); return {"ok": True}

        import insflow.engine.subscriptions as mod
        monkeypatch.setattr(mod, "get_action_router", lambda: FakeRouter())

        result = await svc.dispatch_daily()
        assert result["sent"] == 1
        assert sent and "每日洞察汇总" in sent[0] or sent
