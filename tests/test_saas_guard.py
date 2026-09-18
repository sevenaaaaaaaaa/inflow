"""测试 SaaS 守卫（R4-1：INSFLOW_SAAS=1 控制台要求登录）"""

import pytest

import insflow.core.files as files_mod


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)
    monkeypatch.setenv("INSFLOW_MASTER_KEY", "mk")
    monkeypatch.setenv("INSFLOW_DISABLE_SCHEDULER", "1")
    monkeypatch.setenv("INSFLOW_SAAS", "1")
    from fastapi.testclient import TestClient

    from insflow.server.app import app
    return TestClient(app)


class TestSaasGuard:
    async def test_console_redirects_to_login(self, client):
        r = client.get("/console", follow_redirects=False)
        assert r.status_code == 303
        assert "/console/login" in r.headers["location"]

    async def test_login_page_public(self, client):
        assert client.get("/console/login").status_code == 200
        assert client.get("/console/register").status_code == 200

    async def test_register_then_access(self, client):
        """注册 → 带会话 Cookie → 控制台可访问"""
        r = client.post("/console/register", data={
            "email": "saas@test.com", "password": "password123",
            "name": "SaaS", "workspace_name": "测试租户",
        }, follow_redirects=False)
        assert r.status_code == 303
        # TestClient 自动保存 Cookie
        r2 = client.get("/console", follow_redirects=False)
        assert r2.status_code == 200

    async def test_api_not_blocked(self, client):
        """守卫只管控制台，REST API 不受影响（API 有独立 Key 体系）"""
        assert client.get("/health").status_code == 200


class TestEmbedGuard:
    async def test_embed_view_exempt_from_session(self, client):
        """嵌入视图由 HMAC 令牌鉴权：无会话也不得被守卫重定向"""
        from insflow.engine.embed import mint
        tok = mint("default", "cockpit:traffic")
        r = client.get("/console/embed", params={"token": tok},
                       follow_redirects=False)
        assert r.status_code != 303          # 不被登录守卫拦截
        assert "login" not in r.headers.get("location", "")
        bad = client.get("/console/embed", params={"token": "x"},
                         follow_redirects=False)
        assert bad.status_code == 403        # 令牌无效 → fail-closed

    async def test_token_mint_requires_login(self, client):
        r = client.get("/console/embed/token", params={"panel": "cockpit:traffic"},
                       follow_redirects=False)
        assert r.status_code in (303, 401)   # 无会话不可签发

    async def test_token_mint_after_login(self, client):
        client.post("/console/register", data={
            "email": "embed@test.com", "password": "password123",
            "name": "E", "workspace_name": "嵌入租户"}, follow_redirects=False)
        r = client.get("/console/embed/token", params={"panel": "cockpit:traffic"})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] and "iframe" in body


class TestCacheAndTenantIsolation:
    async def test_console_responses_not_cacheable(self, client):
        """反代不得缓存带会话的 HTML（否则跨会话命中）"""
        client.post("/console/register", data={
            "email": "cache@test.com", "password": "password123",
            "name": "C", "workspace_name": "缓存租户"}, follow_redirects=False)
        r = client.get("/console")
        assert r.status_code == 200
        assert "no-store" in r.headers.get("cache-control", "")

    async def test_embed_token_scoped_to_session_workspace(self, client):
        """签发令牌使用登录用户的工作区，不取「第一个工作区」（跨租户越权回归）"""
        from fastapi.testclient import TestClient

        from insflow.core.accounts import COOKIE_NAME, AccountManager
        from insflow.core.store import get_store
        from insflow.server.app import app
        # 另一租户（独立会话）先注册 → 库里存在多个工作区
        other = TestClient(app)
        other.post("/console/register", data={
            "email": "a@test.com", "password": "password123",
            "name": "A", "workspace_name": "租户A"}, follow_redirects=False)
        client.post("/console/register", data={
            "email": "b@test.com", "password": "password123",
            "name": "B", "workspace_name": "租户B"}, follow_redirects=False)
        me = await AccountManager().verify_session(client.cookies.get(COOKIE_NAME))
        ws_ids = [w.id for w in await (await get_store()).list_workspaces()]
        assert len(ws_ids) >= 2 and me["workspace_id"] in ws_ids
        assert any(w != me["workspace_id"] for w in ws_ids)   # 存在他租户
        r2 = client.get("/console/embed/token", params={"panel": "cockpit:traffic"})
        assert r2.status_code == 200
        from insflow.engine.embed import verify
        assert verify(r2.json()["token"])["w"] == me["workspace_id"]
