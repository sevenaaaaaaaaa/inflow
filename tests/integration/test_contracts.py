"""跨系统契约测试：四条通道 + 签名 + 幂等 + 失败路径

覆盖 docs/04 / docs/12 的通道定义；peer 是真实 HTTP 服务（见 fake_peer.py），
验证的是契约而非 mock 行为。
"""
import json

import pytest

import insflow.core.files as files_mod
from insflow.core.entities import Action, Insight, Workspace
from insflow.core.store import Store, reset_store
from tests.integration.fake_peer import FakeHTTPPeer


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)
    monkeypatch.setenv("INSFLOW_MASTER_KEY", "mk")
    monkeypatch.setenv("INSFLOW_DISABLE_SCHEDULER", "1")
    from insflow.core.cache import cache as _cache
    _cache.invalidate("")
    s = Store(db_path=tmp_path / "t.db")
    await s.connect()
    await s.migrate()
    reset_store(s)
    await s.create_workspace(Workspace(id="test-ws", name="T"))
    yield {"store": s}
    reset_store(None)
    await s.close()


async def _make_action(env, action_type: str, target_ref: str = "",
                       params: dict | None = None) -> Action:
    store = env["store"]
    ins = await store.create_insight(Insight(workspace_id="test-ws", type="t",
                                             title="洞察", summary="摘要"))
    # Action 实体没有 title/summary 字段（标题来自关联洞察）
    return await store.create_action(Action(
        workspace_id="test-ws", insight_id=ins.id, action_type=action_type,
        target_ref=target_ref, params_json=params or {}))


class TestOutboundOpenFlow:
    """IF → OpenFlow：webhook / automation / plugin 三条通道"""

    def test_insight_webhook_contract(self, env, monkeypatch):
        import asyncio
        peer = FakeHTTPPeer(secret="of-secret")
        base = peer.start()
        try:
            monkeypatch.setenv("OPENFLOW_BASE_URL", base)
            monkeypatch.setenv("OPENFLOW_WEBHOOK_SECRET", "of-secret")
            monkeypatch.setenv("OPENFLOW_INBOUND_CONNECTOR_ID", "conn-1")
            from insflow.actions.router import ActionContext, get_action_router

            async def _run():
                action = await _make_action(env, "openflow.webhook_insight")
                return await get_action_router().dispatch(
                    {"action_type": action.action_type, "title": "洞察",
                     "summary": "摘要"},
                    ActionContext(workspace_id="test-ws", insight_id=action.insight_id))
            res = asyncio.get_event_loop().run_until_complete(_run())
            assert res.get("ok") is True, res
            calls = peer.find("/api/webhook.php")
            assert len(calls) == 1
            assert calls[0]["signature_ok"] is True          # HMAC 契约
            body = calls[0]["json"]
            assert body.get("connector") == "conn-1"
            assert body.get("event") == "insight_received"
            assert body.get("source_system") == "insight-flow"
        finally:
            peer.stop()

    def test_automation_contract_and_explicit_failure(self, env, monkeypatch):
        import asyncio

        from insflow.actions.router import ActionContext, get_action_router

        # 未配置 → 明确失败（不能静默成功）
        for key in ("OPENFLOW_BASE_URL", "OPENFLOW_WEBHOOK_SECRET",
                    "OPENFLOW_PLUGIN_ROUTE"):
            monkeypatch.delenv(key, raising=False)
        async def _dispatch():
            action = await _make_action(env, "openflow.automation",
                                        params={"automation": "wf-1"})
            return await get_action_router().dispatch(
                {"action_type": action.action_type, "params_json": action.params_json},
                ActionContext(workspace_id="test-ws", insight_id=action.insight_id))
        res = asyncio.get_event_loop().run_until_complete(_dispatch())
        assert res.get("ok") is False and "未配置" in str(res)

        # 配置后 → 真发事件（模型产出的 openflow.automation 此前无适配器 → 派发失败）
        peer = FakeHTTPPeer(secret="of-secret")
        base = peer.start()
        try:
            monkeypatch.setenv("OPENFLOW_BASE_URL", base)
            monkeypatch.setenv("OPENFLOW_WEBHOOK_SECRET", "of-secret")
            monkeypatch.setenv("OPENFLOW_INBOUND_CONNECTOR_ID", "conn-1")
            res2 = asyncio.get_event_loop().run_until_complete(_dispatch())
            assert res2.get("ok") is True, res2
            body = peer.find("/api/webhook.php")[0]["json"]
            assert body["event"] == "if.automation_request"
            assert body["automation"] == "wf-1"
        finally:
            peer.stop()

    def test_plugin_route_contract(self, env, monkeypatch):
        import asyncio
        peer = FakeHTTPPeer(secret="of-secret",
                            routes={"/api/plugin/insight-flow/":
                                    {"ok": True, "action": "accepted"}})
        base = peer.start()
        try:
            monkeypatch.setenv("OPENFLOW_BASE_URL", base)
            monkeypatch.setenv("OPENFLOW_WEBHOOK_SECRET", "of-secret")
            monkeypatch.setenv("OPENFLOW_PLUGIN_ROUTE", "trigger")
            from insflow.actions.router import ActionContext, get_action_router
            action = asyncio.get_event_loop().run_until_complete(
                _make_action(env, "openflow.plugin_api", target_ref="trigger"))
            res = asyncio.get_event_loop().run_until_complete(
                get_action_router().dispatch(
                    {"action_type": "openflow.plugin_api",
                     "target_ref": "trigger",
                     "params_json": {"route": "trigger"}},
                    ActionContext(workspace_id="test-ws",
                                  insight_id=action.insight_id)))
            assert res.get("ok") is True, res
            calls = peer.find("/api/plugin/insight-flow/trigger")
            assert calls and calls[0]["signature_ok"] is True
        finally:
            peer.stop()


class TestOutboundMFlow:
    def test_create_content_contract(self, env, monkeypatch):
        import asyncio

        # 适配器在注册时构造 MFlowClient（快照 env）→ 测试需重置单例
        import insflow.actions.router as _router_mod
        _router_mod._router = None

        def _loop_create(record):
            return {"ok": True, "id": "loop-9"}

        peer = FakeHTTPPeer(routes={
            "/api/loop/create": _loop_create,
            "/api/loop/detail": {"ok": True, "id": "loop-9", "status": "done",
                                 "output": "草稿内容"},
        })
        base = peer.start()
        try:
            monkeypatch.setenv("MFLOW_BASE_URL", base)
            monkeypatch.setenv("MFLOW_CONSOLE_PASSWORD", "pw")
            from insflow.actions.router import ActionContext, get_action_router
            res = asyncio.get_event_loop().run_until_complete(
                get_action_router().dispatch(
                    {"action_type": "mflow.create_content", "title": "选题",
                     "summary": "brief",
                     "params_json": {"topic": "选题", "max_polls": 1,
                                     "poll_interval": 0.01}},
                    ActionContext(workspace_id="test-ws", insight_id="i1")))
            assert res.get("ok") is True, res
            assert res.get("ref", "").startswith("mflow:loop:")
        finally:
            peer.stop()


class TestInboundContracts:
    """OpenFlow/MFlow → IF：事件入站（签名 + 幂等 + 落库）"""

    def _post(self, payload: dict, secret: str = "ing-secret"):
        import hashlib
        import hmac

        from fastapi.testclient import TestClient

        from insflow.server.app import app
        raw = json.dumps(payload, ensure_ascii=False).encode()
        sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        return TestClient(app).post(
            "/api/v1/ingest", params={"workspace_id": "test-ws"}, content=raw,
            headers={"X-IF-Signature": sig, "Content-Type": "application/json"})

    def test_cdp_order_lands_and_dedupes(self, env, monkeypatch):
        import asyncio
        monkeypatch.setenv("INSFLOW_INGEST_SECRET", "ing-secret")
        payload = {"event": "cdp.order", "event_id": "evt-100",
                   "source": "openflow",
                   "data": {"identity": "buyer@x.com", "amount": 88.5,
                            "channel": "search", "ts": "2026-09-10T10:00:00+00:00"}}
        first = self._post(payload)
        assert first.status_code == 200
        body = first.json()
        assert body["recorded"] is True and body["journey_events"] == 1
        again = self._post(payload).json()
        assert again.get("duplicate") is True                 # 幂等契约

        async def _counts():
            store = env["store"]
            j = await store.list_journey_events("test-ws")
            m = await store._fetchall(
                "SELECT metric, value FROM metrics WHERE workspace_id='test-ws'")
            return j, {r["metric"]: r["value"] for r in m}
        journey, metrics = asyncio.get_event_loop().run_until_complete(_counts())
        assert len(journey) == 1                              # 重放未双写
        assert metrics.get("order_revenue") == 88.5
        assert metrics.get("order_count") == 1.0

    def test_bad_signature_rejected(self, env, monkeypatch):
        monkeypatch.setenv("INSFLOW_INGEST_SECRET", "ing-secret")
        r = self._post({"event": "cdp.lead", "event_id": "e2"}, secret="wrong")
        assert r.status_code in (200, 401)
        assert r.json().get("ok") is False
        assert "签名" in str(r.json().get("error", ""))

    def test_content_published_returns_to_action(self, env, monkeypatch):
        """MFlow 发布回流 → 关联动作进入验证（闭环最后一公里）"""
        import asyncio
        monkeypatch.setenv("INSFLOW_INGEST_SECRET", "ing-secret")
        asyncio.get_event_loop().run_until_complete(
            _make_action(env, "mflow.create_content",
                         params={"item_id": "item-7"}))
        r = self._post({"event": "content.published", "event_id": "pub-1",
                        "item_id": "item-7", "topic": "选题", "url": "https://x/1"})
        assert r.status_code == 200 and r.json().get("ok") is True
