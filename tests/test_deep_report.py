"""测试深度报告生成器（多 Agent 协作，顾问交付物）"""

import pytest

from insflow.core.entities import Insight, Workspace
from insflow.core.store import Store, get_store, reset_store
from insflow.engine.deep_report import DeepReportBuilder


@pytest.fixture
async def env(tmp_path, monkeypatch):
    import insflow.core.files as files_mod

    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)

    s = Store(db_path=tmp_path / "test.db")
    await s.connect()
    await s.migrate()
    reset_store(s)
    await s.create_workspace(Workspace(id="test-ws", name="Test WS"))

    # 预置各类洞察（三类分析师各有信号）
    for t in ("traffic_anomaly", "competitor_pricing", "journey_content_gap"):
        await s.create_insight(Insight(
            workspace_id="test-ws", type=t,
            title=f"{t} 信号", summary="测试",
            severity="high", confidence=0.8,
            evidence_json=[{"type": "t"}],
            actions=[{"action_type": "investigate", "description": f"处理 {t}"}],
        ))

    yield {"store": s}

    reset_store(None)
    await s.close()


class TestDeepReport:
    async def test_collect_context(self, env):
        builder = DeepReportBuilder("test-ws")
        data = await builder.collect_context()
        assert data["workspace"]["stage"] == "S0"
        assert len(data["insights"]) == 3
        assert data["feedback"] == {}

    async def test_analysts_finding(self, env):
        builder = DeepReportBuilder("test-ws")
        data = await builder.collect_context()

        traffic = await builder._traffic_analyst(data)
        competitor = await builder._competitor_analyst(data)
        journey = await builder._journey_analyst(data)

        assert traffic["signals"]
        assert competitor["signals"]
        assert journey["signals"]
        assert all(a["conclusion"] for a in (traffic, competitor, journey))

    async def test_build_report(self, env):
        builder = DeepReportBuilder("test-ws")
        result = await builder.build()

        assert result["report_path"]
        assert set(result["analysts"]) == {"traffic-analyst", "competitor-analyst", "journey-analyst"}

        from insflow.core.files import ReportStore
        reports = ReportStore("test-ws").list_reports("deep-dive")
        assert len(reports) == 1
        content = reports[0].read_text(encoding="utf-8")
        assert "深度诊断报告" in content
        assert "执行摘要" in content
        assert "Top5 行动建议" in content
        assert "90 天路线图" in content
        assert "[ins:" in content  # 溯源

    async def test_empty_workspace(self, env):
        from insflow.core.entities import Workspace as WS
        store = await get_store()
        await store.create_workspace(WS(id="empty", name="Empty"))

        builder = DeepReportBuilder("empty")
        result = await builder.build()
        assert result["report_path"]
