"""批次 F：AI native —— 语义检索 / Prompt 版本化与回归 / 模型路由 / 流式问答"""

import json

import pytest
from fastapi.testclient import TestClient

from insflow.agent import InsightAgent
from insflow.agent.llm import BudgetLedger, LLMGateway, price_of
from insflow.agent.prompt_eval import judge, load_cases, run_eval
from insflow.agent.prompts import get_prompt, list_prompts, list_versions
from insflow.core.entities import Insight, Workspace
from insflow.core.store import Store, get_store, reset_store
from insflow.engine import semantic_index as si
from insflow.server.app import app


@pytest.fixture
async def env(tmp_path, monkeypatch):
    """临时环境 + 两条主题不同的洞察（用于验证语义检索能分辨）"""
    import insflow.core.files as files_mod
    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("INSFLOW_EMBED_MODEL", raising=False)

    s = Store(db_path=tmp_path / "test.db")
    await s.connect()
    await s.migrate()
    reset_store(s)
    await s.create_workspace(Workspace(id="test-ws", name="Test WS"))

    pricing = await s.create_insight(Insight(
        workspace_id="test-ws", type="competitor_pricing",
        title="竞品A基础版降价 20%",
        summary="竞品A 把基础版从 $99 下调到 $79，定价压力上升",
        severity="high", confidence=0.9,
        evidence_json=[{"type": "pricing_change", "old_price": 99, "new_price": 79}]))
    traffic = await s.create_insight(Insight(
        workspace_id="test-ws", type="traffic_drop",
        title="自然搜索会话数下滑",
        summary="ga4_sessions 近 7 天环比下降 32%，集中在移动端落地页",
        severity="medium", confidence=0.7,
        evidence_json=[{"type": "metric", "metric": "ga4_sessions"}]))

    yield {"store": s, "pricing": pricing, "traffic": traffic, "tmp": tmp_path}

    reset_store(None)
    await s.close()


class TestLocalVector:
    def test_deterministic_across_calls(self):
        a = si.embed_local("竞品降价")
        b = si.embed_local("竞品降价")
        assert a == b and si.cosine(a, b) == pytest.approx(1.0, abs=1e-6)

    def test_related_beats_unrelated(self):
        q = si.embed_local("对手是不是降价了")
        near = si.embed_local("竞品A基础版降价 20%，定价下调")
        far = si.embed_local("服务器磁盘告警与备份恢复演练")
        assert si.cosine(q, near) > si.cosine(q, far)

    def test_chinese_bigrams_do_not_cross_punctuation(self):
        # "好。的" 不该产出二字 "好的"（跨标点拼词会制造假命中）
        tokens = {t for t, _ in si._weighted_tokens("好。的")}
        assert "好的" not in tokens
        assert {"好", "的"} <= tokens

    def test_synonym_rewrite_is_a_known_limit(self):
        """诚实边界：字面 n-gram 召不回同义改写，这正是 embedding API 选项存在的理由"""
        doc = si.embed_local("竞品A基础版降价 20%，定价压力上升")
        assert si.cosine(si.embed_local("竞品降价了吗"), doc) > si.MIN_SCORE
        assert si.cosine(si.embed_local("对手把价钱压下来了"), doc) < si.MIN_SCORE

    def test_dense_and_sparse_cosine(self):
        assert si.cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
        assert si.cosine({0: 1.0}, [1.0, 0.0]) == pytest.approx(1.0)


class TestSemanticIndex:
    async def test_sync_then_search(self, env):
        report = await si.sync_workspace("test-ws")
        assert report["indexed"] == 2

        hits = await si.search("test-ws", "竞品降价了吗", kinds=["insight"])
        assert hits and hits[0]["ref_id"] == env["pricing"].id

    async def test_incremental_skips_unchanged(self, env):
        await si.sync_workspace("test-ws")
        again = await si.sync_workspace("test-ws")
        assert again["indexed"] == 0 and again["skipped"] == 2

    async def test_orphan_rows_removed(self, env):
        await si.sync_workspace("test-ws")
        store = await get_store()
        await store._execute("DELETE FROM insights WHERE id = ?", (env["traffic"].id,))
        await store._db.commit()
        report = await si.sync_workspace("test-ws")
        assert report["removed"] == 1
        hits = await si.search("test-ws", "会话数下滑", kinds=["insight"])
        assert all(h["ref_id"] != env["traffic"].id for h in hits)

    async def test_model_switch_reembeds(self, env, monkeypatch):
        await si.sync_workspace("test-ws")
        # 换 embedding 模型：文本没变，但向量必须重算，否则检索被 model 过滤成空
        monkeypatch.setenv("INSFLOW_EMBED_MODEL", "text-embedding-3-small")
        report = await si.sync_workspace("test-ws")
        assert report["indexed"] == 2 and report["skipped"] == 0

    async def test_notes_are_searchable(self, env):
        from insflow.engine.agent_memory import save_note
        await save_note("test-ws", title="定价策略共识",
                        body="我们不跟进竞品降价，改走服务捆绑")
        hits = await si.search("test-ws", "要不要跟着降价", kinds=["note"])
        assert hits and hits[0]["kind"] == "note"

    async def test_reindex_rebuilds(self, env):
        await si.sync_workspace("test-ws")
        report = await si.reindex("test-ws")
        assert report["indexed"] == 2
        st = await si.stats("test-ws")
        assert st["total"] == 2 and st["models"] == [si.LOCAL_MODEL]

    async def test_empty_query_returns_nothing(self, env):
        assert await si.search("test-ws", "   ") == []


class TestSearchSurfaces:
    async def test_agent_tool(self, env):
        from insflow.agent.tools import execute_tool
        data = json.loads(await execute_tool(
            "search_insights", {"workspace_id": "test-ws", "query": "降价压力"}))
        assert data["total"] >= 1
        assert data["insights"][0]["id"] == env["pricing"].id
        assert data["insights"][0]["score"] > 0

    async def test_mcp_tool_registered(self, env):
        from insflow.mcp_server.tools import MCP_TOOLS_SCHEMA, call_mcp_tool
        assert "search_insights" in {t["name"] for t in MCP_TOOLS_SCHEMA}
        out = json.loads(await call_mcp_tool(
            "search_insights", {"workspace_id": "test-ws", "query": "降价"}))
        assert out["total"] >= 1

    async def test_api_search_and_reindex(self, env):
        cli = TestClient(app)
        r = cli.get("/api/v1/search", params={"workspace_id": "test-ws", "q": "降价压力"})
        assert r.status_code == 200 and r.json()["total"] >= 1

        r2 = cli.post("/api/v1/search/reindex",
                      params={"workspace_id": "test-ws", "full": True})
        assert r2.status_code == 200 and r2.json()["stats"]["total"] == 2

    async def test_api_unknown_workspace_404(self, env):
        r = TestClient(app).get("/api/v1/search",
                                params={"workspace_id": "nope", "q": "x"})
        assert r.status_code == 404


class TestPromptRegistry:
    def test_agent_system_v1_on_disk(self):
        assert 1 in list_versions("agent_system")
        assert any(p["name"] == "agent_system" for p in list_prompts())

    def test_hash_is_stable_and_ref_readable(self):
        p1 = get_prompt("agent_system")
        p2 = get_prompt("agent_system")
        assert p1.hash == p2.hash
        assert p1.ref == f"agent_system@v{p1.version}#{p1.hash}"

    def test_env_pins_version(self, monkeypatch):
        monkeypatch.setenv("INSFLOW_PROMPT_AGENT_SYSTEM", "1")
        assert get_prompt("agent_system").version == 1

    def test_fallback_when_dir_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("INSFLOW_PROMPTS_DIR", str(tmp_path / "nope"))
        p = get_prompt("agent_system", fallback="兜底文案")
        assert p.version == 0 and p.text == "兜底文案"

    async def test_answer_carries_prompt_ref(self, env):
        agent = InsightAgent("test-ws", llm=LLMGateway(api_key=""))
        result = await agent.ask("最近有什么洞察")
        assert result["prompt"]["version"] >= 1
        assert result["prompt"]["ref"].startswith("agent_system@v")


class TestPromptEval:
    def test_golden_set_loaded(self):
        cases = load_cases()
        assert len(cases) >= 5
        assert all(c.get("question") for c in cases)

    def test_judge_rules(self):
        case = {"id": "x", "expect_any": ["洞察"], "forbid": ["已派发"],
                "expect_citation": True}
        assert judge(case, "有 3 条洞察 [ins:abc]", [])["passed"]
        assert not judge(case, "有 3 条洞察", [])["passed"]          # 缺引用
        assert not judge(case, "已派发 [ins:a]", [{"insight_id": "a"}])["passed"]
        assert not judge(case, "什么都没有 [ins:a]", [{"insight_id": "a"}])["passed"]

    async def test_run_eval_retrieval_mode(self, env):
        report = await run_eval("test-ws")
        assert report["mode"] == "retrieval"
        # 没 LLM 时测的是答案管线，不是 prompt 文案——报告必须自曝这一点
        assert report["prompt_sensitive"] is False
        assert report["skipped"] >= 1        # llm-only 用例被跳过
        assert report["score"] == 1.0

    async def test_eval_report_written(self, env):
        report = await run_eval("test-ws", write=True)
        assert report["path"] and report["path"].endswith(".json")


class TestModelRouting:
    def test_tier_mapping(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "x")
        gw = LLMGateway(api_key="x")
        gw.model_cheap, gw.model_strong = "cheap-m", "strong-m"
        assert gw.resolve_model("reason") == ("strong-m", "strong", False)
        assert gw.resolve_model("summarize") == ("cheap-m", "cheap", False)
        assert gw.resolve_model("classify")[1] == "cheap"

    def test_budget_degrades_strong_to_cheap(self):
        gw = LLMGateway(api_key="x", budget=BudgetLedger(max_cost_usd=1.0))
        gw.model_cheap, gw.model_strong, gw.degrade_at = "cheap-m", "strong-m", 0.7
        gw.budget.total_cost_usd = 0.8
        model, tier, degraded = gw.resolve_model("reason")
        assert (model, tier, degraded) == ("cheap-m", "cheap", True)

    def test_no_silent_switch_when_single_model(self):
        gw = LLMGateway(api_key="x", model="only-m", budget=BudgetLedger(max_cost_usd=1.0))
        gw.budget.total_cost_usd = 0.99
        assert gw.resolve_model("reason") == ("only-m", "strong", False)

    def test_price_prefix_match_and_cost(self):
        assert price_of("gpt-4o-mini-2024-07-18") == (0.15, 0.60)
        assert price_of("完全没听过的模型") == (0.50, 1.50)
        gw = LLMGateway(api_key="x")
        cost = gw._estimate_cost({"prompt_tokens": 1_000_000,
                                  "completion_tokens": 0}, "gpt-4o")
        assert cost == pytest.approx(2.50)

    def test_price_override_from_env(self, monkeypatch):
        monkeypatch.setenv("INSFLOW_MODEL_PRICES", json.dumps({"my-m": [1.0, 2.0]}))
        assert price_of("my-m-0715") == (1.0, 2.0)

    def test_ledger_tracks_degradation(self):
        from insflow.agent.llm import LLMUsage
        ledger = BudgetLedger(max_cost_usd=2.0)
        ledger.record(LLMUsage(cost_usd=0.5, model="strong-m", tier="strong"))
        ledger.record(LLMUsage(cost_usd=0.1, model="cheap-m", tier="cheap", degraded=True))
        assert ledger.degraded_calls == 1
        assert ledger.used_ratio() == pytest.approx(0.3)
        assert set(ledger.by_model()) == {"strong-m", "cheap-m"}

    async def test_routing_api(self, env):
        r = TestClient(app).get("/api/v1/agent/routing",
                                params={"workspace_id": "test-ws"})
        body = r.json()
        assert r.status_code == 200 and body["available"] is False
        assert body["task_tiers"]["summarize"] == "cheap"
        assert body["degrade_at"] > 0


class TestAskStream:
    async def test_stream_events_retrieval_mode(self, env):
        agent = InsightAgent("test-ws", llm=LLMGateway(api_key=""))
        events = [ev async for ev in agent.ask_stream("最近有什么洞察")]
        kinds = [e[0] for e in events]
        assert kinds[0] == "stage" and kinds[-1] == "done"
        assert "delta" in kinds

        streamed = "".join(d["text"] for k, d in events if k == "delta")
        done = events[-1][1]
        assert streamed == done["answer"]
        assert done["mode"] == "retrieval" and done["citations"]
        assert done["prompt"]["ref"].startswith("agent_system@")

    async def test_stream_matches_non_stream_answer(self, env):
        agent = InsightAgent("test-ws", llm=LLMGateway(api_key=""))
        once = await agent.ask("竞品最近有什么动作")
        events = [ev async for ev in agent.ask_stream("竞品最近有什么动作")]
        assert events[-1][1]["answer"] == once["answer"]

    async def test_sse_endpoint(self, env):
        r = TestClient(app).get("/api/v1/agent/stream",
                                params={"workspace_id": "test-ws",
                                        "question": "最近有什么洞察"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        body = r.text
        assert "event: stage" in body and "event: delta" in body
        assert "event: done" in body

    async def test_sse_unknown_workspace(self, env):
        r = TestClient(app).get("/api/v1/agent/stream",
                                params={"workspace_id": "nope", "question": "x"})
        assert r.status_code == 404
