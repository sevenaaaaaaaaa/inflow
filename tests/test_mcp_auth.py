"""测试 MCP HTTP 端点认证（R2-4 fail-closed）"""

import pytest

import insflow.server.app as app_mod


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("INSFLOW_DISABLE_SCHEDULER", "1")
    monkeypatch.delenv("INSFLOW_API_AUTH", raising=False)
    app_mod.AUTH_ENABLED = True
    app_mod.MCP_AUTH_REQUIRED = True
    from fastapi.testclient import TestClient

    from insflow.server.app import app
    yield TestClient(app)
    app_mod.AUTH_ENABLED = False
    app_mod.MCP_AUTH_REQUIRED = True


class TestMCPAuth:
    async def test_anonymous_rejected_by_default(self, client):
        """fail-closed：默认要求认证，匿名 401"""
        app_mod.MCP_AUTH_REQUIRED = True
        r = client.get("/api/v1/mcp/tools")
        assert r.status_code == 401

    async def test_valid_key_allowed(self, client, tmp_path, monkeypatch):
        import insflow.core.files as fm
        monkeypatch.setattr(fm, "DATA_DIR", tmp_path)
        app_mod.MCP_AUTH_REQUIRED = True

        app_mod._auth_managers.pop("test-ws", None)
        key = app_mod._get_auth("test-ws").create_key("ci", scopes=["read"])

        r = client.get("/api/v1/mcp/tools?workspace_id=test-ws",
                       headers={"X-API-Key": key.key})
        assert r.status_code == 200
        assert r.json()["auth_required"] is True

    async def test_read_only_key_cannot_trigger(self, client, tmp_path, monkeypatch):
        """trigger_playbook 属写操作 → read scope 403/401"""
        import insflow.core.files as fm
        monkeypatch.setattr(fm, "DATA_DIR", tmp_path)
        app_mod.MCP_AUTH_REQUIRED = True

        app_mod._auth_managers.pop("test-ws", None)
        key = app_mod._get_auth("test-ws").create_key("ro", scopes=["read"])

        r = client.post("/api/v1/mcp/tools/call?workspace_id=test-ws",
                        headers={"X-API-Key": key.key},
                        json={"name": "trigger_playbook",
                              "arguments": {"workspace_id": "ws",
                                            "insight_id": "i",
                                            "action_type": "feishu.notify"}})
        assert r.status_code in (401, 403)

    async def test_local_debug_opt_out(self, client, monkeypatch):
        """本地调试显式 INSFLOW_MCP_AUTH=0 才放开"""
        monkeypatch.setenv("INSFLOW_MCP_AUTH", "0")
        app_mod.MCP_AUTH_REQUIRED = False
        r = client.get("/api/v1/mcp/tools")
        assert r.status_code == 200
        assert r.json()["auth_required"] is False
