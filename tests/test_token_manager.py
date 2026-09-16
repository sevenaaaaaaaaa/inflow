from datetime import timedelta, datetime, timezone
"""测试 OAuth token 自动轮换（R1-1）"""

from datetime import UTC, datetime, timedelta

import pytest

from insflow.engine.token_manager import TokenManager, TokenRefreshError


@pytest.fixture
async def tm(monkeypatch, tmp_path):
    import insflow.core.files as fm
    from insflow.core.entities import Workspace
    from insflow.core.store import Store, reset_store

    monkeypatch.setattr(fm, "DATA_DIR", tmp_path)
    monkeypatch.setenv("INSFLOW_MASTER_KEY", "test-master-key")

    s = Store(db_path=tmp_path / "t.db")
    await s.connect()
    await s.migrate()
    reset_store(s)
    await s.create_workspace(Workspace(id="test-ws", name="T"))

    yield TokenManager("test-ws", client_id="cid", client_secret="csecret")

    reset_store(None)
    await s.close()


def fresh_tokens(**overrides):
    t = {
        "access_token": "old-at",
        "refresh_token": "rt-1",
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }
    t.update(overrides)
    return t


class TestEnsureFresh:
    async def test_valid_token_passthrough(self, tm):
        tm.save_tokens("gsc", fresh_tokens(access_token="live"))
        assert tm.ensure_fresh("gsc") == "live"

    async def test_no_credentials_raises(self, tm):
        with pytest.raises(TokenRefreshError) as e:
            tm.ensure_fresh("gsc")
        assert "未授权" in str(e.value)

    async def test_refresh_on_expiry_window(self, tm, monkeypatch):
        """临期（<10min）→ 自动轮换 + 保险库回写 + expires_at 更新"""
        capture = {}
        soon = datetime.now(UTC) + timedelta(seconds=300)
        tm.save_tokens("gsc", fresh_tokens(access_token="old", expires_at=soon.isoformat()))

        import insflow.engine.token_manager as mod

        captured = {}

        def fake_post(url, data=None, headers=None, timeout=None):
            captured["url"] = url
            captured["data"] = data
            return FakeSyncResp({"access_token": "new-at", "expires_in": 3600})

        monkeypatch.setattr(mod.httpx, "post", fake_post)
        token = tm.ensure_fresh("gsc")

        assert token == "new-at"
        assert captured["data"]["grant_type"] == "refresh_token"
        assert captured["data"]["refresh_token"] == "rt-1"
        saved = tm.load_tokens("gsc")
        assert saved["access_token"] == "new-at"
        assert datetime.fromisoformat(saved["expires_at"]) > datetime.now(UTC) + timedelta(minutes=50)

    async def test_old_record_without_expiry_trusted(self, tm):
        """旧格式（无 expires_at，v1.0 遗留）：信任至 401 降级"""
        tm.save_tokens("gsc", {"access_token": "legacy", "refresh_token": "rt"})
        assert tm.ensure_fresh("gsc") == "legacy"

    async def test_no_refresh_token_fails_with_event(self, tm):
        tm.save_tokens("gsc", {"access_token": ""})  # 无 access_token 且无 refresh_token
        with pytest.raises(TokenRefreshError) as e:
            tm.ensure_fresh("gsc")
        assert "refresh_token" in str(e.value)


class FakeSyncResp:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError(f"{self.status_code}", request=None, response=None)

    def json(self):
        return self._data


class TestForceRefresh:
    async def test_401_fallback_rotates(self, tm, monkeypatch):
        """401 降级：无条件轮换，grant_type=refresh_token"""
        capture = {}
        tm.save_tokens("gsc", fresh_tokens(access_token="stale"))

        import insflow.engine.token_manager as mod

        def fake_post(url, data=None, headers=None, timeout=None):
            capture["data"] = data
            return FakeSyncResp({"access_token": "fresh", "expires_in": 3600})

        monkeypatch.setattr(mod.httpx, "post", fake_post)
        token = tm.force_refresh("gsc")

        assert token == "fresh"
        assert capture["data"]["grant_type"] == "refresh_token"
        assert tm.load_tokens("gsc")["access_token"] == "fresh"

    async def test_invalid_grant_requires_reauth(self, tm, monkeypatch):
        """refresh_token 被撤销 → 明确报错要求重新授权（事件进入告警链路）"""
        tm.save_tokens("gsc", fresh_tokens(refresh_token="dead"))
        events = []

        def fake_post(url, data=None, headers=None, timeout=None):
            return FakeSyncResp({"error": "invalid_grant",
                                 "error_description": "Token has been expired or revoked."},
                                status=400)

        class FakeBus:
            @staticmethod
            def emit(t, p):
                events.append((t, p))

        import insflow.engine.token_manager as mod
        monkeypatch.setattr(mod.httpx, "post", fake_post)
        monkeypatch.setattr(tm, "bus", type("B", (), {"emit": staticmethod(lambda t, p: events.append((t, p)))})())

        with pytest.raises(TokenRefreshError) as e:
            tm.force_refresh("gsc")
        assert "重新授权" in str(e.value)
        # 失败事件已记录（供告警出站消费）
        assert any(ev[0] == "source.token_refresh_failed" for ev in events)

    async def test_no_client_credentials(self, tm, monkeypatch):
        tm2 = TokenManager("test-ws", client_id="", client_secret="")
        tm2.save_tokens("gsc", fresh_tokens())
        with pytest.raises(TokenRefreshError) as e:
            tm2.force_refresh("gsc")
        assert "GOOGLE_OAUTH" in str(e.value)


class TestExpiryAudit:
    async def test_expiry_status_states(self, tm, tmp_path):
        from datetime import timedelta as td
        # ok：剩余 30 天
        tm.save_tokens("gsc", fresh_tokens(
            expires_at=(datetime.now(timezone.utc) + timedelta(days=30)).isoformat()))
        assert tm.expiry_status("gsc")["state"] == "ok"
        # expiring_soon：剩余 3 天
        soon = (datetime.now(timezone.utc) + td(days=3)).isoformat()
        tm.save_tokens("ga4", fresh_tokens(expires_at=soon))
        assert tm.expiry_status("ga4")["state"] == "expiring_soon"

    async def test_expired_state(self, tm):
        tm.save_tokens("gsc", fresh_tokens(
            expires_at=(datetime.now(timezone.utc) - td(days=1) if False else
                        datetime.now(timezone.utc) - timedelta(days=1)).isoformat()))
        assert tm.expiry_status("gsc")["state"] == "expired"

    async def test_missing_and_legacy(self, tm):
        assert tm.expiry_status("ga4")["state"] == "missing"  # 未保存
        tm.save_tokens("gsc", {"access_token": "x", "refresh_token": "rt"})  # 旧格式
        assert tm.expiry_status("gsc")["state"] == "legacy"

    async def test_audit_all_emits_expiring_event(self, tm, tmp_path, monkeypatch):
        import insflow.core.files as fm
        monkeypatch.setattr(fm, "DATA_DIR", tmp_path)
        monkeypatch.setenv("INSFLOW_MASTER_KEY", "mk")
        events = []
        monkeypatch.setattr(tm, "bus", type("B", (), {"emit": staticmethod(lambda t, p: events.append((t, p)))})())

        soon = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        tm.save_tokens("gsc", fresh_tokens(expires_at=soon))

        statuses = tm.audit_all()
        assert len(statuses) == 2
        assert any(ev[0] == "auth.token_expiring" for ev in events)
        assert tm.expiry_status("gsc")["state"] == "expiring_soon"
