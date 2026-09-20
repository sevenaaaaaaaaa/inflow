"""批次 E：控制台主题令牌 / 动作插件化 / 自定义看板"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import insflow.core.files as files_mod
from insflow.actions.router import get_action_router, reset_action_router
from insflow.core.entities import Workspace
from insflow.core.store import Store, reset_store
from insflow.engine.plugin_scaffold import create_plugin
from insflow.engine.theme_tokens import normalize_tokens, tokens_to_css
from insflow.server.app import app


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)
    monkeypatch.setenv("INSFLOW_MASTER_KEY", "e-key")
    monkeypatch.setenv("INSFLOW_DISABLE_SCHEDULER", "1")
    s = Store(db_path=tmp_path / "t.db")
    await s.connect()
    await s.migrate()
    reset_store(s)
    await s.create_workspace(Workspace(id="test-ws", name="E"))
    yield {"store": s, "tmp": tmp_path}
    reset_store(None)
    await s.close()


class TestThemeTokens:
    def test_sanitize_rejects_injection(self):
        bad = normalize_tokens({"accent": "url(javascript:alert(1))",
                                "radius": "hack", "density": "xxl"})
        assert bad["accent"] == ""
        assert bad["radius"] == "default"
        assert bad["density"] == "default"
        assert tokens_to_css(bad) == ""

    def test_hex_and_radius_emit_css(self):
        css = tokens_to_css({"accent": "#ff6600", "radius": "compact"})
        assert "--accent:#ff6600" in css
        assert "--r-lg:16px" in css
        assert "javascript" not in css

    async def test_console_injects_css(self, env):
        from insflow.engine.theme_tokens import save_tokens
        await save_tokens("test-ws", {"accent": "#112233", "density": "compact"})
        html = TestClient(app).get("/console", params={"workspace_id": "test-ws"}).text
        assert "if-theme-tokens" in html
        assert "--accent:#112233" in html
        assert 'data-density="compact"' in html

    async def test_api_roundtrip(self, env):
        cli = TestClient(app)
        r = cli.put("/api/v1/theme", json={
            "workspace_id": "test-ws", "accent": "#00aa88",
            "radius": "relaxed", "density": "comfortable"})
        assert r.status_code == 200 and r.json()["ok"]
        got = cli.get("/api/v1/theme", params={"workspace_id": "test-ws"}).json()
        assert got["tokens"]["accent"] == "#00aa88"
        assert got["tokens"]["radius"] == "relaxed"


class TestActionPlugins:
    async def test_scaffold_registers(self, env, monkeypatch, tmp_path):
        import insflow.engine.marketplace as mp
        plugins = tmp_path / "plugins"
        (plugins / "actions").mkdir(parents=True)
        monkeypatch.setattr(mp, "PLUGINS_DIR", plugins)
        dest = plugins / "actions" / "ping-hook"
        result = create_plugin("action", "ping-hook", dest=dest, name="Ping")
        assert result["ok"] and result["check"]["passed"]
        reset_action_router()
        types = get_action_router().list_types()
        assert "custom.ping-hook" in types
        adapters = get_action_router().list_adapters()
        plugin_row = next(a for a in adapters if a["action_type"] == "custom.ping-hook")
        assert plugin_row["source"] == "plugin"
        api = TestClient(app).get("/api/v1/actions/types").json()
        assert "custom.ping-hook" in api["action_types"]
        reset_action_router()

    async def test_builtin_not_overridden(self, env, monkeypatch, tmp_path):
        import insflow.engine.marketplace as mp
        from insflow.actions.plugins import load_action_plugins
        plugins = tmp_path / "plugins" / "actions" / "steal"
        plugins.mkdir(parents=True)
        monkeypatch.setattr(mp, "PLUGINS_DIR", tmp_path / "plugins")
        (plugins / "manifest.json").write_text(json.dumps({
            "id": "steal", "type": "action", "name": "Steal",
            "version": "0.1.0", "entry": "entry.py",
        }))
        (plugins / "entry.py").write_text(
            "from insflow.actions.router import ActionAdapter, ActionResult\n"
            "class S(ActionAdapter):\n"
            "    @property\n"
            "    def action_type(self):\n"
            "        return 'feishu.notify'\n"
            "    async def execute(self, action, ctx):\n"
            "        return ActionResult.ok()\n"
            "def create_adapter():\n"
            "    return S()\n")
        reset_action_router()
        reports = load_action_plugins(get_action_router(), tmp_path / "plugins" / "actions")
        assert any(not r["ok"] and "占用" in r.get("detail", "") for r in reports)
        reset_action_router()


class TestCustomBoards:
    async def test_crud_and_page(self, env):
        cli = TestClient(app)
        created = cli.post("/api/v1/boards", json={
            "workspace_id": "test-ws", "name": "增长周会",
            "panel_ids": ["traffic.channels", "overview.all", "nope"]}).json()
        assert created["ok"]
        bid = created["board"]["id"]
        assert "traffic.channels" in created["board"]["panel_ids"]
        assert "nope" not in created["board"]["panel_ids"]
        listed = cli.get("/api/v1/boards", params={"workspace_id": "test-ws"}).json()
        assert any(b["id"] == bid for b in listed["boards"])
        page = cli.get(f"/console/boards/{bid}", params={"workspace_id": "test-ws"})
        assert page.status_code == 200
        assert "增长周会" in page.text
        index = cli.get("/console/boards", params={"workspace_id": "test-ws"})
        assert index.status_code == 200 and "自定义看板" in index.text
        assert cli.delete(f"/api/v1/boards/{bid}",
                          params={"workspace_id": "test-ws"}).json()["ok"]

    async def test_embed_custom_board(self, env):
        from insflow.engine.custom_boards import save_board
        from insflow.engine.embed import mint
        board = await save_board("test-ws", name="交付看板",
                                 panel_ids=["overview.all"], board_id="handoff")
        token = mint("test-ws", f"custom:{board['id']}")
        html = TestClient(app).get("/console/embed", params={"token": token}).text
        assert "交付看板" in html or "handoff" in html
        assert "只读嵌入" in html
