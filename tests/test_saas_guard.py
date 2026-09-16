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
