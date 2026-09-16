"""测试 RBAC + API Key 认证（PL-6）"""

from datetime import datetime, timedelta, timezone

import pytest

import insflow.core.files as files_mod
from insflow.core.auth import (
    ACTION_MIN_ROLE,
    APIKey,
    AuthManager,
    Role,
    can,
    mask_key,
    role_at_least,
    utcnow,
    verify_hmac_signature,
)


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)


class TestRBAC:
    def test_role_hierarchy(self):
        assert role_at_least("owner", "analyst")
        assert role_at_least("admin", "admin")
        assert not role_at_least("analyst", "admin")
        assert not role_at_least("viewer", "analyst")

    def test_can_matrix(self):
        assert can("viewer", "read")
        assert not can("viewer", "diagnose")
        assert can("analyst", "diagnose")
        assert not can("analyst", "manage_models")
        assert can("admin", "manage_models")
        assert not can("admin", "manage_workspace")
        assert can("owner", "manage_workspace")

    def test_unknown_action_denied(self):
        assert not can("owner", "unknown_action")

    def test_invalid_role(self):
        assert not role_at_least("root", "viewer")


class TestAPIKey:
    def test_create_and_authenticate(self, tmp_path):
        from insflow.core.auth import AuthManager
        mgr = AuthManager("test-ws")
        key = mgr.create_key("ci", scopes=["read"])

        found = mgr.authenticate(key.key)
        assert found is not None
        assert found.key_id == key.key_id

    def test_invalid_key_rejected(self):
        from insflow.core.auth import AuthManager
        mgr = AuthManager("test-ws")
        mgr.create_key("a", ["read"])
        assert mgr.authenticate("ifk_wrong") is None

    def test_scope_enforcement(self):
        from insflow.core.auth import AuthManager
        mgr = AuthManager("test-ws")
        read_key = mgr.create_key("ro", scopes=["read"])
        write_key = mgr.create_key("rw", scopes=["read", "write"])

        ok, _ = mgr.authorize(read_key.key, "read")
        assert ok
        ok, _ = mgr.authorize(read_key.key, "dispatch_action")  # write 级
        assert not ok
        ok, _ = mgr.authorize(write_key.key, "dispatch_action")
        assert ok

    def test_expired_key_rejected(self):
        from insflow.core.auth import APIKey, AuthManager
        mgr = AuthManager("test-ws")
        key = mgr.create_key("expired", ["read"], ttl_days=1)
        key.expires_at = utcnow() - timedelta(days=1)
        assert mgr.authenticate(key.key) is None

    def test_revoked_rejected(self):
        from insflow.core.auth import AuthManager
        mgr = AuthManager("test-ws")
        key = mgr.create_key("to-revoke", ["read"])
        mgr.revoke(key.key_id)
        assert mgr.authenticate(key.key) is None
        assert all(not k["enabled"] for k in mgr.list_keys() if k["key_id"] == key.key_id)

    def test_mask(self):
        masked = mask_key("ifk_abcdefghijklmnop")
        assert "abcdefghij" not in masked
        assert masked.startswith("ifk_"[:4]) or "****" in masked


class TestHMAC:
    def test_verify(self):
        raw = b"body"
        from insflow.core.auth import hash_secret
        sig = __import__("hashlib").sha256  # noqa
        import hashlib
        import hmac as h
        expected = h.new(b"s", raw, hashlib.sha256).hexdigest()
        assert verify_hmac_signature(raw, expected, "s")
        assert not verify_hmac_signature(raw, expected, "other")

    def test_empty_fail_closed(self):
        assert not verify_hmac_signature(b"x", "sig", "")
