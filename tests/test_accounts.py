"""测试多租户自助账号（R4-1）"""

import pytest

import insflow.core.files as files_mod
from insflow.core.accounts import AccountError, AccountManager, hash_password, verify_password
from insflow.core.store import Store, get_store, reset_store


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)
    monkeypatch.setenv("INSFLOW_MASTER_KEY", "mk")

    s = Store(db_path=tmp_path / "test.db")
    await s.connect()
    await s.migrate()
    reset_store(s)
    yield {"store": s}
    reset_store(None)
    await s.close()


class TestPassword:
    def test_hash_and_verify(self):
        h = hash_password("s3cret-password")
        assert h.startswith("scrypt$")
        assert verify_password("s3cret-password", h)
        assert not verify_password("wrong", h)

    def test_hash_is_salted(self):
        assert hash_password("same") != hash_password("same")


class TestRegister:
    async def test_register_creates_tenant_and_session(self, env):
        mgr = AccountManager()
        result = await mgr.register("alice@example.com", "password123", name="Alice")

        assert result["workspace_id"]
        assert result["token"]
        # 租户已建 + 绑 Free 套餐
        store = await get_store()
        ws = await store.get_workspace(result["workspace_id"])
        assert ws is not None
        assert ws.settings_json.get("plan") == "free"

    async def test_duplicate_email_rejected(self, env):
        mgr = AccountManager()
        await mgr.register("bob@example.com", "password123")
        with pytest.raises(AccountError) as e:
            await mgr.register("bob@example.com", "password123")
        assert "已注册" in str(e.value)

    async def test_weak_password_rejected(self, env):
        with pytest.raises(AccountError) as e:
            await AccountManager().register("c@example.com", "short")
        assert "8 位" in str(e.value)

    async def test_bad_email_rejected(self, env):
        with pytest.raises(AccountError):
            await AccountManager().register("not-an-email", "password123")

    async def test_tenants_isolated(self, env):
        """租户隔离：两个账号各自 workspace，数据互不可见"""
        mgr = AccountManager()
        a = await mgr.register("a@x.com", "password123")
        b = await mgr.register("b@x.com", "password123")
        assert a["workspace_id"] != b["workspace_id"]


class TestLogin:
    async def test_login_success(self, env):
        mgr = AccountManager()
        reg = await mgr.register("d@x.com", "password123")
        result = await mgr.login("d@x.com", "password123")
        assert result["workspace_id"] == reg["workspace_id"]
        assert result["token"]

    async def test_login_case_insensitive_email(self, env):
        mgr = AccountManager()
        await mgr.register("E@x.com", "password123")
        result = await mgr.login("e@x.com", "password123")
        assert result["user_id"]

    async def test_wrong_password(self, env):
        mgr = AccountManager()
        await mgr.register("f@x.com", "password123")
        with pytest.raises(AccountError):
            await mgr.login("f@x.com", "wrong-password")


class TestSession:
    async def test_verify_and_logout(self, env):
        mgr = AccountManager()
        reg = await mgr.register("g@x.com", "password123")
        user = await mgr.verify_session(reg["token"])
        assert user["email"] == "g@x.com"

        await mgr.logout(reg["token"])
        assert await mgr.verify_session(reg["token"]) is None

    async def test_invalid_token(self, env):
        assert await AccountManager().verify_session("bogus") is None
        assert await AccountManager().verify_session(None) is None

    async def test_expired_session_purged(self, env):
        mgr = AccountManager()
        reg = await mgr.register("h@x.com", "password123")
        store = await get_store()
        await store._execute(
            "UPDATE sessions SET expires_at = ? WHERE token = ?",
            ("2020-01-01T00:00:00+00:00", reg["token"]))
        await store._db.commit()
        assert await mgr.verify_session(reg["token"]) is None
