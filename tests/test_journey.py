"""测试客户旅程模块（框架/热力图/断点清单）"""

import pytest

from insflow.core.entities import Workspace
from insflow.core.store import Store, get_store, reset_store
from insflow.engine.journey import JourneyModule, get_framework


@pytest.fixture
async def module(tmp_path, monkeypatch):
    import insflow.core.files as files_mod

    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)

    s = Store(db_path=tmp_path / "test.db")
    await s.connect()
    await s.migrate()
    reset_store(s)
    await s.create_workspace(Workspace(id="test-ws", name="Test WS"))

    yield JourneyModule("test-ws")

    reset_store(None)
    await s.close()


class TestFrameworks:
    def test_default_framework(self):
        fw = get_framework()
        assert fw["name"] == "See-Think-Do-Care"
        assert [s["id"] for s in fw["stages"]] == ["see", "think", "do", "care"]

    def test_aida(self):
        fw = get_framework("aida")
        assert len(fw["stages"]) == 4

    def test_unknown_raises(self):
        with pytest.raises(ValueError):
            get_framework("nope")


class TestHeatmap:
    def test_touchpoint_mapping(self, module):
        result = module.map_touchpoints([
            {"kind": "article", "title": "什么是增长自动化（科普）", "stage_hint": "see"},
            {"kind": "article", "title": "工具评测：A vs B"},
            {"kind": "page", "title": "定价页", "stage_hint": "do"},
        ])
        assert result["coverage"]["see"]["count"] == 1
        assert result["coverage"]["do"]["count"] == 1
        assert result["heatmap"]["think"] < 1.0
        assert "think" in result["hollow_stages"] or result["hollow_stages"]

    def test_hollow_stages_detected(self, module):
        result = module.map_touchpoints([
            {"kind": "article", "title": "科普", "stage_hint": "see"},
        ])
        # 全部阶段触点数 < 3 → 都算空心（含 see）
        assert set(result["hollow_stages"]) == {"see", "think", "do", "care"}


class TestGapInsights:
    async def test_gap_insights_created(self, module):
        result = module.map_touchpoints([
            {"kind": "article", "title": "科普", "stage_hint": "see"},
        ])
        insights = await module.build_gap_insights(result)
        # 4 个阶段全空心 → 4 条洞察
        assert len(insights) == 4
        types = {i.type for i in insights}
        assert types == {"journey_content_gap"}

        # 验证动作映射 MFlow
        actions = [a.action_type for a in insights[0].actions_json]
        assert "mflow.create_content" in actions
