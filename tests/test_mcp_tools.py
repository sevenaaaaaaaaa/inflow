"""测试 MCP Server 完整版（10 工具 + stdio/HTTP 共用层）"""

import json

import pytest

from insflow.core.entities import Insight, Workspace
from insflow.core.store import Store, reset_store
from insflow.mcp_server.tools import MCP_TOOLS_SCHEMA, TOOL_IMPLS, call_mcp_tool


@pytest.fixture
async def env(tmp_path, monkeypatch):
    import insflow.core.files as files_mod

    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)

    s = Store(db_path=tmp_path / "test.db")
    await s.connect()
    await s.migrate()
    reset_store(s)
    await s.create_workspace(Workspace(id="test-ws", name="Test WS"))

    await s.create_insight(Insight(
        workspace_id="test-ws",
        type="competitor_pricing",
        title="竞品B涨价",
        summary="测试",
        evidence_json=[{"type": "pricing_change", "competitor": "竞品B"}],
    ))

    yield {"store": s}

    reset_store(None)
    await s.close()


class TestToolRegistry:
    def test_11_tools_with_schema(self):
        names = {t["name"] for t in MCP_TOOLS_SCHEMA}
        assert {
            "list_insights", "get_insight", "run_diagnosis", "ask_analyst",
            "list_competitors", "get_competitor_timeline", "get_maturity",
            "list_monitors", "trigger_playbook", "get_feedback_stats",
            "propose_action",
        } == names
        assert len(TOOL_IMPLS) == 11

    def test_schemas_have_required(self):
        for t in MCP_TOOLS_SCHEMA:
            assert "inputSchema" in t and "description" in t


class TestToolCalls:
    async def test_list_insights(self, env):
        output = await call_mcp_tool("list_insights", {"workspace_id": "test-ws"})
        data = json.loads(output)
        assert data["total"] >= 1

    async def test_get_insight(self, env):
        store = env["store"]
        insights = await store.list_insights("test-ws")
        output = await call_mcp_tool("get_insight", {"insight_id": insights[0].id})
        data = json.loads(output)
        assert data["title"]

    async def test_get_maturity(self, env):
        output = await call_mcp_tool("get_maturity", {"workspace_id": "test-ws"})
        data = json.loads(output)
        assert data["stage"] == "S0"
        assert data["maturity_level"] == "L0"

    async def test_list_monitors(self, env):
        output = await call_mcp_tool("list_monitors", {"workspace_id": "test-ws"})
        data = json.loads(output)
        assert data["monitors"] == []

    async def test_list_competitors(self, env):
        output = await call_mcp_tool("list_competitors", {"workspace_id": "test-ws"})
        data = json.loads(output)
        assert data["total"] >= 1
        assert data["competitors"][0]["id"] == "竞品B"

    async def test_get_competitor_timeline(self, env):
        output = await call_mcp_tool("get_competitor_timeline",
                                     {"workspace_id": "test-ws", "competitor": "竞品B"})
        data = json.loads(output)
        assert len(data["timeline"]) == 1

    async def test_feedback_stats(self, env):
        output = await call_mcp_tool("get_feedback_stats", {"workspace_id": "test-ws"})
        data = json.loads(output)
        assert isinstance(data, dict)

    async def test_ask_analyst(self, env, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        output = await call_mcp_tool("ask_analyst",
                                     {"workspace_id": "test-ws", "question": "最近有什么洞察"})
        data = json.loads(output)
        assert data["mode"] == "retrieval"
        assert data["citations"]

    async def test_unknown_tool(self, env):
        output = await call_mcp_tool("nope", {})
        assert "Unknown tool" in output

    async def test_diagnosis_runs(self, env):
        output = await call_mcp_tool("run_diagnosis", {"workspace_id": "test-ws"})
        data = json.loads(output)
        assert "insights_created" in data
        assert "report_path" in data

    async def test_trigger_playbook_unknown_insight(self, env):
        output = await call_mcp_tool("trigger_playbook", {
            "workspace_id": "test-ws", "insight_id": "ghost",
            "action_type": "feishu.notify",
        })
        assert "not found" in output
