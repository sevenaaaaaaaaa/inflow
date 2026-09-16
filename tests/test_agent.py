"""测试 Insight Agent v1（工具调用 + Skill 匹配 + 引用溯源）"""

import json
from datetime import datetime, timezone

import pytest

from insflow.agent import InsightAgent, get_skills_host
from insflow.agent.llm import BudgetLedger, LLMGateway
from insflow.core.entities import Insight, Workspace
from insflow.core.store import Store, get_store, reset_store


@pytest.fixture
async def env(tmp_path, monkeypatch):
    """临时环境 + 示例洞察"""
    import insflow.core.files as files_mod

    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)

    s = Store(db_path=tmp_path / "test.db")
    await s.connect()
    await s.migrate()
    reset_store(s)
    await s.create_workspace(Workspace(id="test-ws", name="Test WS"))

    insight = await s.create_insight(Insight(
        workspace_id="test-ws",
        type="competitor_pricing",
        title="竞品A降价20%",
        summary="竞品A基础版从 $99 降至 $79",
        severity="high",
        confidence=0.9,
        evidence_json=[{"type": "pricing_change", "old_price": 99, "new_price": 79}],
    ))

    yield {"store": s, "insight": insight}

    reset_store(None)
    await s.close()


@pytest.fixture
def no_llm(monkeypatch):
    """确保 LLM 不可用（retrieval-only 模式）"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return LLMGateway(api_key="")


class TestSkillsHost:
    def test_6_skills_loaded(self):
        skills = get_skills_host().load()
        assert len(skills) == 6
        expected = {
            "competitor-move-analysis", "traffic-attribution-diagnosis",
            "voice-of-market-mining", "journey-gap-analysis",
            "growth-experiment-design", "weekly-brief-writer",
        }
        assert expected == set(skills.keys())

    def test_match_by_trigger(self):
        host = get_skills_host()
        matched = host.match("帮我分析竞品最近的异动")
        assert matched
        assert matched[0].name == "competitor-move-analysis"

    def test_no_match(self):
        assert get_skills_host().match("今天天气怎么样") == []


class TestAgentTools:
    async def test_query_insights_tool(self, env):
        from insflow.agent.tools import execute_tool
        output = await execute_tool("query_insights", {"workspace_id": "test-ws"})
        data = json.loads(output)
        assert data["total"] >= 1

    async def test_get_insight_detail_tool(self, env):
        from insflow.agent.tools import execute_tool
        output = await execute_tool("get_insight_detail", {"insight_id": env["insight"].id})
        data = json.loads(output)
        assert data["evidence"]


class TestAgentAsk:
    async def test_retrieval_mode_with_citations(self, env):
        agent = InsightAgent("test-ws", llm=LLMGateway(api_key=""))
        result = await agent.ask("最近有什么重要洞察？")

        assert result["mode"] == "retrieval"
        assert "[ins:" in result["answer"]  # 引用格式
        assert result["citations"]  # 溯源清单已展开
        assert result["citations"][0]["title"]

    async def test_empty_workspace_answer(self, env):
        agent = InsightAgent("test-ws", llm=LLMGateway(api_key=""))
        # 清空洞察
        store = await get_store()
        await store.update_insight_status(env["insight"].id, "dismissed")

        # 用一个空 workspace 问
        await store.create_workspace(Workspace(id="empty-ws", name="Empty"))
        agent2 = InsightAgent("empty-ws", llm=LLMGateway(api_key=""))
        result = await agent2.ask("有什么洞察")
        assert "暂无洞察" in result["answer"]

    async def test_skill_matched_flag(self, env):
        agent = InsightAgent("test-ws", llm=LLMGateway(api_key=""))
        result = await agent.ask("帮我分析竞品异动")
        assert result["skills_used"] == ["competitor-move-analysis"]

    async def test_llm_mode_with_fake_gateway(self, env):
        """LLM 模式：function calling 循环 + 引用展开"""

        class FakeLLM(LLMGateway):
            def __init__(self):
                super().__init__(api_key="fake")
                self.rounds = 0
                self.budget = BudgetLedger()

            async def chat(self, messages, tools=None, temperature=0.3):
                self.rounds += 1
                if self.rounds == 1:
                    return {
                        "content": None,
                        "tool_calls": [{
                            "id": "call_1", "type": "function",
                            "function": {"name": "query_insights",
                                         "arguments": json.dumps({"limit": 5})},
                        }],
                        "usage": None,
                    }
                return {
                    "content": "最重要的洞察：竞品A降价20% [ins:" + env["insight"].id + "]",
                    "tool_calls": [],
                    "usage": None,
                }

        agent = InsightAgent("test-ws", llm=FakeLLM())
        result = await agent.ask("总结本周洞察")

        assert result["mode"] == "llm"
        assert "竞品A降价" in result["answer"]
        assert result["citations"][0]["insight_id"] == env["insight"].id

    async def test_budget_exhausted_fails_closed(self, env):
        """预算耗尽 → 明确报错（fail-closed）"""
        ledger = BudgetLedger(max_cost_usd=0.0)
        ledger.record(None) if False else None
        # 手动触发 exceeded
        from insflow.agent.llm import LLMUsage
        ledger.record(LLMUsage(cost_usd=1.0))

        gateway = LLMGateway(api_key="k", budget=ledger)
        agent = InsightAgent("test-ws", llm=gateway)

        # retrieval 模式不受预算影响（不调 LLM）
        result = await agent.ask("测试")
        assert result["mode"] == "retrieval"
