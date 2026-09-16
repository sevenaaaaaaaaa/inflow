"""测试成熟度评估模块（DM-1 问卷 + DM-4 阶段判定 + 报告）"""

import pytest

from insflow.engine.maturity import QUESTIONNAIRE, MaturityEngine


@pytest.fixture
async def engine(tmp_path, monkeypatch):
    """成熟度引擎（数据目录与全局 store 重定向到临时目录）"""
    import insflow.core.files as files_mod
    from insflow.core.entities import Workspace
    from insflow.core.store import Store, reset_store

    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)

    s = Store(db_path=tmp_path / "test.db")
    await s.connect()
    await s.migrate()
    reset_store(s)
    await s.create_workspace(Workspace(id="test-ws", name="Test WS"))

    yield MaturityEngine("test-ws")

    reset_store(None)
    await s.close()


class TestQuestionnaire:
    async def test_all_dimensions_present(self):
        """问卷应有 5 维度，共 30 题"""
        assert len(QUESTIONNAIRE) == 5
        total = sum(len(d["questions"]) for d in QUESTIONNAIRE.values())
        assert total == 30

    async def test_score_all_zeros(self, engine):
        result = engine.score({})
        assert result["level"] == "L0"
        assert result["overall_pct"] == 0.0

    async def test_score_all_max(self, engine):
        answers = {}
        for dim in QUESTIONNAIRE.values():
            for q in dim["questions"]:
                answers[q["id"]] = 3
        result = engine.score(answers)
        assert result["level"] == "L4"
        assert result["total"] == result["max"]

    async def test_score_mid(self, engine):
        answers = {}
        for dim in QUESTIONNAIRE.values():
            for q in dim["questions"]:
                answers[q["id"]] = 2  # 2/3 ≈ 66.7% → L3
        result = engine.score(answers)
        assert result["level"] == "L3"

    async def test_radar_shape(self, engine):
        result = engine.score({"inst_1": 2})
        assert set(result["radar"].keys()) == {
            "instrumentation", "toolstack", "governance", "attribution", "automation"
        }


class TestStageDetermination:
    async def test_early_stage(self, engine):
        result = engine.determine_stage(0.2, months_since_launch=3)
        assert result["stage"] == "S0"

    async def test_small_traffic(self, engine):
        result = engine.determine_stage(0.5, monthly_sessions=500, conversion_rate=0.005)
        assert result["engine_stage"] == "S0"

    async def test_growth_stage(self, engine):
        result = engine.determine_stage(0.6, monthly_sessions=5000)
        assert result["engine_stage"] == "S1"

    async def test_lifecycle_stage(self, engine):
        result = engine.determine_stage(0.7, monthly_sessions=50000, conversion_rate=0.015)
        assert result["engine_stage"] == "S2"

    async def test_commercial_stage(self, engine):
        result = engine.determine_stage(0.9, monthly_sessions=50000, conversion_rate=0.03)
        assert result["engine_stage"] == "S3"

    async def test_user_confirmation_priority(self, engine):
        result = engine.determine_stage(
            0.5, monthly_sessions=5000, user_confirmed_stage="S2"
        )
        assert result["stage"] == "S2"
        assert result["conflict"]  # 有冲突提示


class TestReportAndAssess:
    async def test_build_report_contains_top5(self, engine):
        score = engine.score({})
        stage = engine.determine_stage(0.0)
        report = engine.build_report(score, stage)
        assert "# 数据成熟度报告" in report
        assert "Top5 补课清单" in report

    @pytest.mark.asyncio
    async def test_assess_writes_back(self, engine):
        """评估后 stage/maturity 应写回 workspace"""
        answers = {q["id"]: 2 for dim in QUESTIONNAIRE.values() for q in dim["questions"]}
        result = await engine.assess(answers, monthly_sessions=5000)

        assert result["score"]["level"] == "L3"
        assert result["report_path"]

        from insflow.core.entities import MaturityLevel
        from insflow.core.store import get_store
        store = await get_store()
        ws = await store.get_workspace("test-ws")
        assert ws.maturity_level == MaturityLevel.L3
