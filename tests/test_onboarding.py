"""测试第一方接入向导（OAuth 流程 + 凭据保险库 + 健康检查）"""

import base64
from urllib.parse import parse_qs, urlparse

import pytest

import insflow.core.files as files_mod
from insflow.core.entities import Workspace
from insflow.core.store import Store, reset_store
from insflow.engine.onboarding import OAuthState, OnboardingService


@pytest.fixture
async def env(tmp_path, monkeypatch):
    """保险库重定向 + 必需主密钥"""
    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)
    monkeypatch.setenv("INSFLOW_MASTER_KEY", "test-master-key")

    s = Store(db_path=tmp_path / "test.db")
    await s.connect()
    await s.migrate()
    reset_store(s)
    await s.create_workspace(Workspace(id="test-ws", name="Test WS"))

    svc = OnboardingService("test-ws")
    yield {"store": s, "service": svc, "tmp": tmp_path}

    reset_store(None)
    await s.close()


class TestAuthURL:
    def test_gsc_url(self, env):
        svc = env["service"]
        url = svc.auth_url("gsc", client_id="cid.apps.googleusercontent.com",
                           redirect_uri="https://if.test/oauth/callback")
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        assert "accounts.google.com" in parsed.netloc
        assert qs["client_id"] == ["cid.apps.googleusercontent.com"]
        assert "webmasters.readonly" in qs["scope"][0]
        assert qs["access_type"] == ["offline"]  # 需要 refresh_token
        assert qs["state"][0]

    def test_ga4_scope(self, env):
        url = env["service"].auth_url("ga4", "cid", "https://if.test/cb")
        assert "analytics.readonly" in url

    def test_unknown_provider(self, env):
        with pytest.raises(ValueError):
            env["service"].auth_url("mystery", "cid", "https://if.test/cb")


class TestOAuthCallback:
    async def test_state_one_time_and_expiry(self, env, monkeypatch):
        """state：一次性 + 5 分钟过期 + provider 匹配"""
        svc = env["service"]
        state = OAuthState.create("test-ws", "gsc")

        # 模拟过期
        OAuthState._sessions[state]["created_at"] -= 400
        result = await svc.exchange_code("gsc", "code-x", state, "cid", "csecret",
                                         "https://if.test/cb")
        assert result["ok"] is False
        assert "过期" in result["error"]

    async def test_state_mismatch(self, env):
        svc = env["service"]
        state = OAuthState.create("test-ws", "ga4")
        result = await svc.exchange_code("gsc", "code", state, "cid", "csecret", "uri")
        assert result["ok"] is False
        assert "不匹配" in result["error"]

    async def test_state_replay_rejected(self, env, monkeypatch):
        """state 只能用一次（防重放）"""
        from insflow.engine.onboarding import GOOGLE_TOKEN_URL

        svc = env["service"]
        state = OAuthState.create("test-ws", "gsc")

        class FakeResp:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {"access_token": "at123", "refresh_token": "rt456", "expires_in": 3600}

        class FakeAsyncClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return None
            async def post(self, url, data=None, headers=None, timeout=None):
                return FakeResp()

        import insflow.engine.onboarding as mod
        monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **kw: FakeAsyncClient())

        ok = await svc.exchange_code("gsc", "real-code", state, "cid", "csecret", "uri")
        assert ok["ok"] is True
        assert ok["has_refresh"] is True

        # 重放：state 已消费
        replay = await svc.exchange_code("gsc", "real-code", state, "cid", "s", "uri")
        assert replay["ok"] is False

    async def test_tokens_saved_to_vault(self, env, monkeypatch):
        from insflow.core.security import get_vault
        from insflow.engine.onboarding import GOOGLE_TOKEN_URL

        svc = env["service"]
        state = OAuthState.create("test-ws", "gsc")

        class FakeResp:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {"access_token": "at123", "refresh_token": "rt456"}

        class FakeAsyncClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return None
            async def post(self, url, data=None, headers=None, timeout=None):
                return FakeResp()

        import insflow.engine.onboarding as mod
        monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **kw: FakeAsyncClient())

        await svc.exchange_code("gsc", "code", state, "cid", "csecret", "uri")

        vault = get_vault()
        raw = vault.get("gsc_oauth_tokens")
        import json
        tokens = json.loads(raw)
        assert tokens["access_token"] == "at123"
        assert tokens["refresh_token"] == "rt456"
        # token 不含明文（AES-GCM 加密落盘）
        vault_file = (files_mod.DATA_DIR / "vault.json").read_text()
        assert "at123" not in vault_file  # 加密后明文不可见


class TestAPICredential:
    async def test_save_crux_key(self, env):
        svc = env["service"]
        result = await svc.save_api_credential("crux", "AIza-crux-key")
        assert result["ok"] is True
        from insflow.core.security import get_vault
        assert get_vault().get("crux_api_key") == "AIza-crux-key"

    async def test_unknown_provider_rejected(self, env):
        result = await env["service"].save_api_credential("mystery", "x")
        assert result["ok"] is False
