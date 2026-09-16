"""测试 /api/v1/ingest 入站（HMAC 验签 + MFlow 发布回流 → 验证状态机）"""

import hashlib
import hmac as hmac_mod
import json

import pytest

from insflow.actions.ingest import IngestReceiver, verify_signature
from insflow.core.entities import Action, ActionState, Insight, Workspace
from insflow.core.store import Store, get_store, reset_store


def sign(body: bytes, secret: str) -> str:
    return hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()


class FakeHeaders(dict):
    """大小写不敏感的请求头"""

    def get(self, key, default=None):
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


@pytest.fixture
async def env(tmp_path, monkeypatch):
    """临时环境 + ingest secret"""
    import insflow.core.files as files_mod

    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)
    monkeypatch.setenv("INSFLOW_INGEST_SECRET", "ingest-secret-1")

    s = Store(db_path=tmp_path / "test.db")
    await s.connect()
    await s.migrate()
    reset_store(s)
    await s.create_workspace(Workspace(id="test-ws", name="Test WS"))

    insight = await s.create_insight(Insight(
        workspace_id="test-ws",
        type="competitor_pricing",
        title="竞品降价",
        summary="测试",
        evidence_json=[{"type": "t"}],
    ))
    action = await s.create_action(Action(
        workspace_id="test-ws",
        insight_id=insight.id,
        action_type="mflow.create_content",
        description="产稿",
        target_ref="",
    ))

    receiver = IngestReceiver("test-ws")
    yield {"store": s, "insight": insight, "action": action, "receiver": receiver}

    reset_store(None)
    await s.close()


class TestSignature:
    def test_verify_ok(self):
        body = b'{"event":"content.published"}'
        sig = sign(body, "s")
        assert verify_signature(body, sig, "s")

    def test_verify_tampered(self):
        body = b'{"event":"content.published"}'
        sig = sign(body, "s")
        assert not verify_signature(body + b"x", sig, "s")

    def test_verify_empty_secret(self):
        assert not verify_signature(b"x", "sig", "")


class TestIngestAuth:
    async def test_rejects_without_secret(self, env, monkeypatch):
        monkeypatch.delenv("INSFLOW_INGEST_SECRET", raising=False)
        receiver = IngestReceiver("test-ws")
        result = await receiver.handle(b"{}", FakeHeaders(), "")
        assert result["ok"] is False
        assert "fail-closed" in result["error"]

    async def test_rejects_bad_signature(self, env):
        receiver = env["receiver"]
        result = await receiver.handle(b'{"event":"x"}', FakeHeaders({"X-IF-Signature": "bad"}), "bad")
        assert result["ok"] is False
        assert "签名" in result["error"]

    async def test_accepts_both_header_names(self, env):
        body = json.dumps({"event": "unknown.event", "x": 1}).encode()
        sig = sign(body, "ingest-secret-1")
        for header_name in ("X-IF-Signature", "X-Inbound-Signature"):
            result = await env["receiver"].handle(body, FakeHeaders({header_name: sig}), sig)
            assert result["ok"] is True


class TestContentPublished:
    async def test_published_completes_action(self, env):
        """发布回流 → 动作 done → 验证状态机"""
        action, receiver = env["action"], env["receiver"]

        # 动作先派发（有基线与窗口）
        from insflow.actions.feedback_tracker import FeedbackTracker
        await FeedbackTracker("test-ws").mark_dispatched(action, {"gsc_clicks": 100})

        body = json.dumps({
            "event": "content.published",
            "insight_id": action.insight_id,
            "item_id": "itm-9",
            "url": "https://blog.example.com/post-1",
        }).encode()
        sig = sign(body, "ingest-secret-1")

        result = await receiver.handle(body, FakeHeaders({"X-IF-Signature": sig}), sig)

        assert result["ok"] is True
        assert result["matched"] is True

        store = await get_store()
        updated = await store.get_action(action.id)
        assert updated.state == ActionState.VERIFYING
        assert updated.result_json["published_url"] == "https://blog.example.com/post-1"

    async def test_published_unmatched_recorded(self, env):
        body = json.dumps({"event": "content.published", "item_id": "unknown"}).encode()
        sig = sign(body, "ingest-secret-1")
        result = await env["receiver"].handle(body, FakeHeaders({"X-IF-Signature": sig}), sig)
        assert result["ok"] is True
        assert result["matched"] is False

    async def test_failed_marks_action(self, env):
        action, receiver = env["action"], env["receiver"]
        await FeedbackTracker("test-ws") if False else None

        from insflow.actions.feedback_tracker import FeedbackTracker
        await FeedbackTracker("test-ws").mark_dispatched(action, {"gsc_clicks": 100})
        store = await get_store()
        action = await store.get_action(action.id)
        # 直接置为 dispatched 状态以便失败路径处理
        await store.update_action_state(action.id, "dispatched")

        body = json.dumps({
            "event": "content.failed",
            "loop_id": "loop-x",
            "error": "generation failed",
        }).encode()
        sig = sign(body, "ingest-secret-1")
        result = await receiver.handle(body, FakeHeaders({"X-IF-Signature": sig}), sig)
        assert result["ok"] is True

    async def test_openflow_events_recorded(self, env):
        body = json.dumps({"event": "cdp_event", "visitor_id": "v1", "page": "/pricing"}).encode()
        sig = sign(body, "ingest-secret-1")
        result = await env["receiver"].handle(body, FakeHeaders({"X-Inbound-Signature": sig}), sig)
        assert result["ok"] is True
        assert result.get("recorded") is True
