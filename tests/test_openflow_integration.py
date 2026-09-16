"""测试 OpenFlow 零插件通道 + Action Router"""

import hashlib
import hmac
import json

import pytest

from insflow.actions.router import (
    ActionContext,
    ActionResult,
    GenericWebhookAdapter,
    OpenFlowWebhookAdapter,
    get_action_router,
)
from insflow.integrations.openflow import OpenFlowClient, sign_body


class TestSignature:
    def test_sign_matches_php_hash_hmac(self):
        """签名算法必须与 OpenFlow InboundReceiver::inbound_verify 一致"""
        raw_body = b'{"connector":"c1","event":"test"}'
        secret = "shared-secret-123"
        sig = sign_body(raw_body, secret)
        expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        assert sig == expected


class TestOpenFlowClient:
    def test_not_configured(self):
        client = OpenFlowClient(base_url="", secret="")
        assert not client.configured

    @pytest.mark.asyncio
    async def test_push_insight(self, monkeypatch, tmp_path):
        """推送洞察 → 验证请求带正确 HMAC 头"""
        captured = {}

        class FakeResp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"ok": True, "event": "insight_received"}

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def post(self, url, content=None, headers=None, timeout=None):
                captured["url"] = url
                captured["body"] = content
                captured["headers"] = headers
                return FakeResp()

        import insflow.integrations.openflow.client as mod
        monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **kw: FakeClient())

        client = OpenFlowClient(
            base_url="https://openflow.example.com",
            secret="s3cret",
        )
        result = await client.push_insight_to_cdp(
            {"id": "abc", "workspace_id": "ws1", "title": "竞品降价"},
            connector_id="conn-insflow",
        )

        assert result["ok"] is True
        assert captured["url"].endswith("/api/webhook.php")
        sig = captured["headers"]["X-Inbound-Signature"]
        assert hmac.compare_digest(sig, sign_body(captured["body"], "s3cret"))
        payload = json.loads(captured["body"])
        assert payload["connector"] == "conn-insflow"
        assert payload["event"] == "insight_received"


class TestActionRouter:
    def test_builtin_adapters_registered(self):
        router = get_action_router()
        assert "openflow.webhook_insight" in router.list_types()
        assert "webhook.generic" in router.list_types()

    @pytest.mark.asyncio
    async def test_dispatch_unknown_type(self, tmp_path, monkeypatch):
        import insflow.core.files as files_mod
        monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)

        router = get_action_router()
        result = await router.dispatch(
            {"action_type": "unknown.adapter"},
            ActionContext(workspace_id="ws", insight_id="i1"),
        )
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_openflow_adapter_not_configured(self, tmp_path, monkeypatch):
        import insflow.core.files as files_mod
        monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)
        monkeypatch.delenv("OPENFLOW_BASE_URL", raising=False)
        monkeypatch.delenv("OPENFLOW_WEBHOOK_SECRET", raising=False)

        adapter = OpenFlowWebhookAdapter(client=OpenFlowClient(base_url="", secret=""))
        result = await adapter.execute(
            {"action_type": "openflow.webhook_insight", "target_ref": "c1"},
            ActionContext(workspace_id="ws", insight_id="i1"),
        )
        assert result["ok"] is False
        assert "未配置" in result["detail"]

    @pytest.mark.asyncio
    async def test_generic_webhook(self, tmp_path, monkeypatch):
        import insflow.core.files as files_mod
        monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)

        captured = {}

        class FakeResp:
            status_code = 200

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def post(self, url, content=None, headers=None, timeout=None):
                captured["url"] = url
                captured["body"] = content
                captured["headers"] = headers
                return FakeResp()

        import insflow.actions.router as mod
        monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **kw: FakeClient())

        adapter = GenericWebhookAdapter()
        result = await adapter.execute(
            {
                "action_type": "webhook.generic",
                "target_ref": "https://n8n.example.com/webhook/abc",
                "params_json": {"secret": "whsec"},
                "description": "转发洞察",
            },
            ActionContext(workspace_id="ws", insight_id="i1"),
        )
        assert result["ok"] is True
        assert captured["url"] == "https://n8n.example.com/webhook/abc"
        # HMAC 头存在且可验证
        sig = captured["headers"]["X-IF-Signature"]
        expected = hmac.new(b"whsec", captured["body"], hashlib.sha256).hexdigest()
        assert sig == expected
