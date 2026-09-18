"""测试第一方情报（MCP 客户端 + CJ-3 旅程重建 + CJ-5 RFM）"""

from datetime import UTC, datetime, timedelta

import pytest

from insflow.core.entities import Workspace
from insflow.core.store import Store, get_store, reset_store
from insflow.engine.first_party import FirstPartyIntelligence, rfm_segment, score_rfm
from insflow.integrations.openflow.mcp_client import OpenFlowMCPClient, OpenFlowMCPError


def days_ago(n: int) -> str:
    return (datetime.now(UTC) - timedelta(days=n)).isoformat()


@pytest.fixture
async def env(tmp_path, monkeypatch):
    import insflow.core.files as files_mod

    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)

    s = Store(db_path=tmp_path / "test.db")
    await s.connect()
    await s.migrate()
    reset_store(s)
    await s.create_workspace(Workspace(id="test-ws", name="Test WS"))

    yield {"store": s}

    reset_store(None)
    await s.close()


# ========== MCP 客户端 ==========

class TestOpenFlowMCPClient:
    def test_readonly_whitelist(self):
        client = OpenFlowMCPClient(base_url="http://x", api_key="k")
        assert client.available
        assert "members_list" in client.READONLY_TOOLS
        assert "article_create" not in client.READONLY_TOOLS

    async def test_write_tool_rejected(self):
        """只读约束：写工具一律拒绝（fail-closed）"""
        client = OpenFlowMCPClient(base_url="http://x", api_key="k")
        with pytest.raises(OpenFlowMCPError):
            await client.call_tool("article_create", {"title": "x", "content": "y"})

    async def test_not_configured(self):
        client = OpenFlowMCPClient(base_url="", api_key="")
        assert not client.available
        with pytest.raises(OpenFlowMCPError):
            await client.call_tool("members_list")

    async def test_jsonrpc_call(self, monkeypatch):
        """JSON-RPC tools/call 格式与响应解析"""
        captured = {}

        class FakeResp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "jsonrpc": "2.0", "id": 1,
                    "result": {"content": [{"type": "text",
                                            "text": '{"total": 42}'}]},
                }

        class FakeAsyncClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def post(self, url, json=None, headers=None, timeout=None):
                captured["url"] = url
                captured["payload"] = json
                captured["headers"] = headers
                return FakeResp()

        import insflow.integrations.openflow.mcp_client as mod
        monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **kw: FakeAsyncClient())

        client = OpenFlowMCPClient(base_url="https://of.test/mcp-server.php", api_key="key1")
        result = await client.call_tool("leads_count")

        assert result == {"total": 42}
        assert captured["payload"]["method"] == "tools/call"
        assert captured["payload"]["params"]["name"] == "leads_count"
        assert captured["headers"]["X-API-Key"] == "key1"


# ========== RFM 评分 ==========

class TestRFMScoring:
    def test_score(self):
        s = score_rfm(5, 3, 600)
        assert s == {"R": 5, "F": 3, "M": 3}

    def test_segments(self):
        assert rfm_segment(5, 5, 5) == "champions"
        assert rfm_segment(1, 4, 4) == "at_risk"
        assert rfm_segment(5, 1, 2) == "promising"
        assert rfm_segment(1, 1, 1) == "lost"


# ========== CJ-3 旅程重建 ==========

class TestJourneyRebuild:
    def test_stages_and_breakpoints(self):
        now = datetime.now(UTC).isoformat()
        members = (
            [{"email": f"lead{i}@x.com", "registered": False, "form_submitted": True} for i in range(30)]  # lead
            + [{"email": f"member{i}@x.com", "registered": True} for i in range(20)]  # member
            + [{"email": f"paying{i}@x.com", "registered": True} for i in range(3)]  # paying
        )
        orders = [
            {"member_email": f"paying{i}@x.com", "amount": 99, "status": "paid",
             "paid_at": now} for i in range(3)
        ]

        fi = FirstPartyIntelligence("test-ws", client=OpenFlowMCPClient(base_url="", api_key=""))
        result = fi.rebuild_journey(members, orders)

        assert result["stages_distribution"]["lead"] == 30
        assert result["stages_distribution"]["member"] == 20
        assert result["stages_distribution"]["paying"] == 3
        # member(20) → paying(3) 转化 15% < 35% → 断点
        bp = {(b["from"], b["to"]): b for b in result["breakpoints"]}
        assert ("member", "paying") in bp
        assert bp[("member", "paying")]["conversion"] == 0.15

    async def test_persist_journey_events(self, env):
        fi = FirstPartyIntelligence("test-ws", client=OpenFlowMCPClient(base_url="", api_key=""))
        datetime.now(UTC).isoformat()
        members = [{"email": f"m{i}@x.com", "registered": True} for i in range(5)]
        rebuilt = fi.rebuild_journey(members, [])

        await fi.persist_journey(rebuilt)
        store = await get_store()
        events = await store.list_journey_events("test-ws")
        assert len(events) >= 4  # 4 个阶段的 aggregate 快照


# ========== CJ-5 RFM 分层 ==========

class TestRFMFull:
    async def test_compute_and_insight(self, env):
        now = datetime.now(UTC)
        members = (
            [{"email": f"risk{i}@x.com", "registered": True} for i in range(10)]
            + [{"email": f"champ{i}@x.com", "registered": True} for i in range(4)]
        )
        orders = (
            # at_risk：历史多单（F≥4）但最近 >180 天未消费
            [{"member_email": f"risk{i}@x.com", "amount": 300, "status": "paid",
              "paid_at": (now - timedelta(days=200)).isoformat()} for i in range(10)
             for _ in range(4)]
            # champions：最近 + 多单 + 高额
            + [{"member_email": f"champ{i}@x.com", "amount": 3000, "status": "paid",
                "paid_at": (now - timedelta(days=3)).isoformat()} for i in range(4)
               for _ in range(4)]
        )

        fi = FirstPartyIntelligence("test-ws", client=OpenFlowMCPClient(base_url="", api_key=""))
        rfm = fi.compute_rfm(members, orders)

        assert "at_risk" in rfm["segments"]
        assert rfm["segments"]["at_risk"]["count"] == 10

        insights = await fi.rfm_to_insights(rfm)
        assert len(insights) == 1
        assert insights[0].type == "rfm_at_risk"
        assert insights[0].severity == "high"

    async def test_full_analysis_injected(self, env):
        """无 MCP 时注入数据模式完整跑通"""
        fi = FirstPartyIntelligence("test-ws", client=OpenFlowMCPClient(base_url="", api_key=""))
        now = datetime.now(UTC)
        members = [{"email": f"u{i}@x.com", "registered": True} for i in range(8)]
        # 6 人历史多单但 200 天未消费 → at_risk（触发流失预警洞察）
        orders = [{"member_email": f"u{i}@x.com", "amount": 500, "status": "paid",
                   "paid_at": (now - timedelta(days=200)).isoformat()} for i in range(6)
                  for _ in range(4)]

        result = await fi.run_first_party_analysis(members=members, orders=orders)

        assert result["data_mode"] == "injected"
        assert result["insights_created"] >= 1
        assert result["journey"]["total"] == 8
