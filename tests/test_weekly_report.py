"""测试周报体系（生成 + MFlow 同步）"""


import pytest

from insflow.core.entities import Insight, Workspace
from insflow.core.store import Store, reset_store
from insflow.engine.weekly_report import WeeklyReportBuilder


@pytest.fixture
async def env(tmp_path, monkeypatch):
    import insflow.core.files as files_mod

    fake_root = tmp_path / "lovart"
    fake_root.mkdir()
    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)
    monkeypatch.setenv("LOVART_LOCAL_DEV_ROOT", str(fake_root))

    s = Store(db_path=tmp_path / "test.db")
    await s.connect()
    await s.migrate()
    reset_store(s)
    await s.create_workspace(Workspace(id="test-ws", name="Test WS"))

    await s.create_insight(Insight(
        workspace_id="test-ws", type="competitor_pricing",
        title="竞品A降价20%", summary="测试洞察",
        severity="high", confidence=0.9,
        evidence_json=[{"type": "t"}],
    ))

    yield {"store": s, "root": fake_root}

    reset_store(None)
    await s.close()


class TestWeeklyReport:
    async def test_build(self, env):
        builder = WeeklyReportBuilder("test-ws")
        result = await builder.build()

        assert result["report_path"]
        assert result["insights_count"] == 1
        from insflow.core.files import ReportStore
        reports = ReportStore("test-ws").list_reports("weekly")
        assert len(reports) == 1
        content = reports[0].read_text(encoding="utf-8")
        assert "Insight Flow 增长周报" in content
        assert "竞品A降价20%" in content
        assert "ins:" in content  # 引用

    async def test_sync_to_mflow(self, env):
        """周报自动落 MFlow `1-2 Insight/Insight Flow Reports/`"""
        builder = WeeklyReportBuilder("test-ws")
        result = await builder.build()

        assert result["mflow_path"]
        mflow_path = env["root"] / "1-2 Insight" / "Insight Flow Reports" / "weekly"
        files = list(mflow_path.glob("*.md"))
        assert len(files) == 1

    async def test_no_mflow_root(self, env, monkeypatch):
        """未配置 LOVART_LOCAL_DEV_ROOT → 跳过同步不报错"""
        monkeypatch.delenv("LOVART_LOCAL_DEV_ROOT", raising=False)
        builder = WeeklyReportBuilder("test-ws")
        result = await builder.build()
        assert result["report_path"]
        assert result["mflow_path"] is None
