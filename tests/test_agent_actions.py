"""批次 C 测试：Agent 会办事 —— 动作提案审批门 / 工作区记忆 / 定时任务 / MCP 写工具

覆盖：
1. propose_action 只起草（pending）不派发；通知链路可用（修掉 alerts._notify 的坏 import）
2. approve 走 Action Router + 基线 + 14 天验证窗口；reject 进 cancelled 且记录原因
3. agent_notes 写入/版本/检索/注入 system prompt
4. Agent 定时任务：创建 → 挂调度 → 立即执行（retrieval 模式）→ 记录 last_run → 删除
5. MCP propose_action：与 Agent 同一条审批链路（不直接派发）
"""

import json

import pytest

from insflow.actions.router import ActionAdapter, ActionResult, get_action_router
from insflow.agent import InsightAgent
from insflow.agent.llm import LLMGateway
from insflow.core.entities import Insight, Workspace
from insflow.core.store import Store, close_store, get_store, reset_store
from insflow.engine.proposals import (
    ProposalError,
    approve_action,
    list_pending,
    propose_action,
    reject_action,
)


class _EchoAdapter(ActionAdapter):
    """测试用动作适配器：可选失败，用于验证 approve 的成功/失败两条路径"""

    action_type = "test.echo"
    fail = False

    async def execute(self, action: dict, ctx) -> dict:
        if _EchoAdapter.fail:
            return ActionResult.fail("模拟执行失败")
        return ActionResult.ok(ref=f"echo:{ctx.workspace_id}", detail="ok")


@pytest.fixture
async def env(tmp_path, monkeypatch):
    import insflow.core.files as files_mod

    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _EchoAdapter.fail = False
    get_action_router().register(_EchoAdapter())

    s = Store(db_path=tmp_path / "test.db")
    await s.connect()
    await s.migrate()
    reset_store(s)
    await s.create_workspace(Workspace(id="test-ws", name="Test WS"))
    insight = await s.create_insight(Insight(
        workspace_id="test-ws", type="competitor_pricing",
        title="竞品A降价20%", summary="竞品A基础版从 $99 降至 $79",
        severity="high", confidence=0.9,
        evidence_json=[{"type": "pricing_change", "competitor": "竞品A"}],
    ))
    yield {"store": s, "insight": insight}
    reset_store(None)
    await s.close()


@pytest.fixture
def no_llm():
    return LLMGateway(api_key="")


class TestProposalChain:
    async def test_propose_only_creates_pending(self, env):
        """提案只入库待审批，不 dispatch（actions 里没有 dispatched 事件）"""
        from insflow.core.files import EventBus
        result = await propose_action(
            "test-ws", insight_id=env["insight"].id, action_type="test.echo",
            rationale="验证提案链路")
        assert result["ok"] and result["state"] == "pending"
        action = await env["store"].get_action(result["action_id"])
        assert action.state.value == "pending"
        assert action.params_json["proposed_by"] == "agent"
        assert action.params_json["rationale"] == "验证提案链路"
        pending = await list_pending("test-ws")
        assert len(pending) == 1
        assert pending[0]["insight_title"] == "竞品A降价20%"
        types = [e["type"] for e in EventBus("test-ws").read(limit=50)]
        assert "action.proposed" in types
        assert "action.dispatched" not in types

    async def test_propose_requires_insight(self, env):
        with pytest.raises(ProposalError):
            await propose_action("test-ws", insight_id="nope",
                                 action_type="test.echo")

    async def test_propose_rejects_unknown_type(self, env):
        with pytest.raises(ProposalError):
            await propose_action("test-ws", insight_id=env["insight"].id,
                                 action_type="not.registered")

    async def test_approve_dispatches_with_verification_window(self, env):
        result = await propose_action(
            "test-ws", insight_id=env["insight"].id, action_type="test.echo",
            rationale="批准路径")
        approved = await approve_action("test-ws", result["action_id"],
                                        actor="tester")
        assert approved["ok"] and approved["state"] == "dispatched"
        action = await env["store"].get_action(result["action_id"])
        assert action.state.value == "dispatched"
        assert action.verify_window_until is not None
        assert action.baseline_json.get("window_days") == 14
        assert action.result_json.get("approved_by") == "tester"
        # 审批后不再是待审批
        assert await list_pending("test-ws") == []
        # 重复批准会被状态机拒绝
        with pytest.raises(ProposalError):
            await approve_action("test-ws", result["action_id"])

    async def test_approve_failure_marks_failed(self, env):
        result = await propose_action(
            "test-ws", insight_id=env["insight"].id, action_type="test.echo")
        _EchoAdapter.fail = True
        approved = await approve_action("test-ws", result["action_id"])
        assert not approved["ok"]
        action = await env["store"].get_action(result["action_id"])
        assert action.state.value == "failed"          # 重试计数 1/3
        assert (action.result_json or {}).get("retries") == 1

    async def test_reject_cancels_with_reason(self, env):
        result = await propose_action(
            "test-ws", insight_id=env["insight"].id, action_type="test.echo")
        rejected = await reject_action("test-ws", result["action_id"],
                                       actor="boss", reason="预算外")
        assert rejected["ok"] and rejected["state"] == "cancelled"
        action = await env["store"].get_action(result["action_id"])
        assert action.state.value == "cancelled"
        assert action.result_json["rejected_by"] == "boss"
        assert action.result_json["reject_reason"] == "预算外"
        with pytest.raises(ProposalError):
            await approve_action("test-ws", result["action_id"])

    async def test_notify_uses_action_router(self, env):
        """回归：alerts._notify 曾误 import engine.router → 永远 skipped"""
        from insflow.engine.alerts import _notify
        out = await _notify("test-ws", "标题", "摘要", ["feishu"], {})
        assert "skipped" not in out

    async def test_agent_tool_proposes_and_tracks(self, env, no_llm):
        agent = InsightAgent("test-ws", llm=no_llm)
        output = await agent.run_tool("propose_action", {
            "insight_id": env["insight"].id, "action_type": "test.echo",
            "rationale": "Agent 工具链路",
        })
        data = json.loads(output)
        assert data["ok"] and data["state"] == "pending"
        assert agent.proposed_actions[0]["action_id"] == data["action_id"]
        result = await agent.ask("最近有什么洞察")
        assert "proposed_actions" in result          # 每次 ask 重新收集（不串会话）


class TestAgentMemory:
    async def test_save_version_and_recall(self, env):
        from insflow.engine.agent_memory import recall_notes, save_note

        note = await save_note("test-ws", title="Q3 北极星",
                               body="Q3 北极星是付费转化，不是注册数",
                               tags=["目标"], citations=[env["insight"].id])
        assert note["version"] == 1
        updated = await save_note("test-ws", title="Q3 北极星",
                                  body="Q3 北极星是付费转化（8 月起口径含试用转正）")
        assert updated["id"] == note["id"]
        assert updated["version"] == 2
        hits = await recall_notes("test-ws", "北极星")
        assert hits and hits[0]["version"] == 2
        assert env["insight"].id in [c.get("insight_id")
                                     for c in hits[0]["citations_json"]]

    async def test_memory_context_injects_prompt(self, env):
        from insflow.engine.agent_memory import memory_context, save_note

        await save_note("test-ws", title="品牌语气",
                        body="对外文案避免使用『颠覆』")
        ctx = await memory_context("test-ws", "帮我写一段文案")
        # 无命中词 → 回退最近记忆（保证上下文总有一点）
        assert "品牌语气" in ctx and "v1" in ctx

    async def test_agent_tools_memory(self, env, no_llm):
        from insflow.agent.tools import execute_tool

        saved = json.loads(await execute_tool("save_note", {
            "workspace_id": "test-ws", "title": "渠道偏好",
            "body": "主投 GSC + 小红书", "citations": [env["insight"].id]}))
        assert saved["ok"] and saved["version"] == 1
        recalled = json.loads(await execute_tool("recall_notes", {
            "workspace_id": "test-ws", "query": "渠道"}))
        assert recalled["total"] >= 1
        assert recalled["notes"][0]["citations"][0]["insight_id"] == env["insight"].id

    async def test_notes_api(self, tmp_path, monkeypatch):
        import insflow.core.files as files_mod
        monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)
        monkeypatch.setenv("INSFLOW_DISABLE_SCHEDULER", "1")
        from httpx import ASGITransport, AsyncClient

        from insflow.server.app import app
        reset_store(None)
        await (await get_store()).create_workspace(
            Workspace(id="api-notes-ws", name="N"))
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as c:
            r = await c.post("/api/v1/agent/notes", json={
                "workspace_id": "api-notes-ws", "title": "API 记忆",
                "body": "来自 API 的测试", "tags": ["api"]})
            assert r.status_code == 200 and r.json()["ok"]
            r2 = await c.get("/api/v1/agent/notes",
                             params={"workspace_id": "api-notes-ws"})
            assert r2.json()["total"] >= 1
            note_id = r2.json()["notes"][0]["id"]
            assert (await c.delete(f"/api/v1/agent/notes/{note_id}",
                                   params={"workspace_id": "api-notes-ws"})
                    ).json()["ok"]
        await close_store()


class TestAgentTasks:
    async def test_create_normalize_and_run(self, env):
        from insflow.engine.agent_tasks import (
            AgentTaskError,
            create_task,
            delete_task,
            normalize_cron,
            run_task,
        )

        assert normalize_cron("daily") == "0 9 * * *"
        with pytest.raises(AgentTaskError):
            normalize_cron("not-a-cron")
        task = await create_task("test-ws", name="每日渠道对比",
                                 question="近 7 天各渠道转化对比", cron="daily",
                                 created_by="tester")
        assert task["id"] and task["cron"] == "0 9 * * *"
        listed = await env["store"].list_agent_tasks("test-ws")
        assert len(listed) == 1
        result = await run_task(task["id"])
        assert result["ok"] and result["mode"] == "retrieval"
        after = await env["store"].get_agent_task(task["id"])
        assert after["last_status"] == "ok" and after["run_count"] == 1
        assert await delete_task("test-ws", task["id"])
        assert await env["store"].list_agent_tasks("test-ws") == []

    async def test_task_tool_and_toggle(self, env, no_llm):
        from insflow.agent.tools import execute_tool
        from insflow.engine.agent_tasks import set_enabled

        out = json.loads(await execute_tool("schedule_task", {
            "workspace_id": "test-ws", "question": "每天看转化", "cron": "weekly"}))
        assert out["ok"] and out["cron"] == "30 9 * * 1"
        paused = await set_enabled("test-ws", out["task_id"], False)
        assert paused["enabled"] is False
        enabled = await set_enabled("test-ws", out["task_id"], True)
        assert enabled["enabled"] is True

    async def test_tasks_api(self, tmp_path, monkeypatch):
        import insflow.core.files as files_mod
        monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)
        monkeypatch.setenv("INSFLOW_DISABLE_SCHEDULER", "1")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        from httpx import ASGITransport, AsyncClient

        from insflow.server.app import app
        reset_store(None)
        await (await get_store()).create_workspace(
            Workspace(id="api-task-ws", name="T"))
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as c:
            created = (await c.post("/api/v1/agent/tasks", json={
                "workspace_id": "api-task-ws", "question": "近 7 天会话趋势",
                "cron": "daily"})).json()
            assert created["ok"] and created["task"]["id"]
            task_id = created["task"]["id"]
            run = (await c.post(f"/api/v1/agent/tasks/{task_id}/run",
                                params={"workspace_id": "api-task-ws"})).json()
            assert run["ok"]
            bad = await c.post("/api/v1/agent/tasks", json={
                "workspace_id": "api-task-ws", "question": "x", "cron": "bad"})
            assert bad.status_code == 400
            assert (await c.delete(f"/api/v1/agent/tasks/{task_id}",
                                   params={"workspace_id": "api-task-ws"})
                    ).json()["ok"]
        await close_store()


class TestMcpProposeAction:
    async def test_mcp_propose_goes_to_pending(self, env):
        from insflow.mcp_server.tools import call_mcp_tool

        output = json.loads(await call_mcp_tool("propose_action", {
            "workspace_id": "test-ws", "insight_id": env["insight"].id,
            "action_type": "test.echo", "rationale": "来自 MCP"}))
        assert output["ok"] and output["state"] == "pending"
        action = await (await get_store()).get_action(output["action_id"])
        assert action.state.value == "pending"
        assert action.params_json["proposed_by"] == "mcp"


class TestProposalApi:
    async def test_pending_approve_api(self, tmp_path, monkeypatch):
        import insflow.core.files as files_mod

        monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)
        monkeypatch.setenv("INSFLOW_DISABLE_SCHEDULER", "1")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        from httpx import ASGITransport, AsyncClient

        from insflow.server.app import app
        reset_store(None)
        store = await get_store()
        await store.create_workspace(Workspace(id="api-ws", name="API WS"))
        insight = await store.create_insight(Insight(
            workspace_id="api-ws", type="competitor_pricing",
            title="API 提案", summary="t", severity="medium", confidence=0.8))
        get_action_router().register(_EchoAdapter())

        proposed = await propose_action(
            "api-ws", insight_id=insight.id, action_type="test.echo")
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as c:
            listed = (await c.get("/api/v1/actions/pending",
                                  params={"workspace_id": "api-ws"})).json()
            assert any(p["action_id"] == proposed["action_id"]
                       for p in listed["actions"])
            approved = (await c.post(
                f"/api/v1/actions/{proposed['action_id']}/approve",
                json={"workspace_id": "api-ws", "actor": "api"})).json()
            assert approved["ok"] and approved["state"] == "dispatched"
            # 非法状态再批准 → 400
            again = await c.post(
                f"/api/v1/actions/{proposed['action_id']}/approve",
                json={"workspace_id": "api-ws"})
            assert again.status_code == 400
        await close_store()


class TestConsole:
    async def test_agent_page_and_pending_section(self, tmp_path, monkeypatch):
        import insflow.core.files as files_mod

        monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)
        monkeypatch.setenv("INSFLOW_DISABLE_SCHEDULER", "1")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        from httpx import ASGITransport, AsyncClient

        from insflow.server.app import app
        reset_store(None)
        store = await get_store()
        await store.create_workspace(Workspace(id="ui-ws", name="UI WS"))
        insight = await store.create_insight(Insight(
            workspace_id="ui-ws", type="competitor_pricing",
            title="UI 提案", summary="t", severity="high", confidence=0.8))
        get_action_router().register(_EchoAdapter())
        await propose_action("ui-ws", insight_id=insight.id,
                             action_type="test.echo", rationale="UI 渲染用例")

        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as c:
            page = await c.get("/console/agent", params={"workspace_id": "ui-ws"})
            assert page.status_code == 200
            assert "Agent 工作台" in page.text
            assert "待审批动作" in page.text and "UI 渲染用例" in page.text
            assert "ifProposalApprove" in page.text and "ifTaskCreate" in page.text
            loop = await c.get("/console/cockpit/action-loop",
                               params={"workspace_id": "ui-ws"})
            assert loop.status_code == 200
            assert "待审批动作" in loop.text and "ifLoopApprove" in loop.text
            # Agent 导航入口
            home = await c.get("/console", params={"workspace_id": "ui-ws"})
            assert home.status_code == 200 and "/console/agent" in home.text
        await close_store()

    def test_js_hooks_present(self):
        from pathlib import Path
        js = Path("insflow/web/static_app.js").read_text(encoding="utf-8")
        for hook in ("ifAskApprove", "ifAskReject", "ifAskSaveNote",
                     "ifAskRenderExtras"):
            assert hook in js
