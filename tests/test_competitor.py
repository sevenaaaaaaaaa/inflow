"""测试竞品档案 + SEO 竞争情报（CI-1/3/7）"""

import pytest

from insflow.core.entities import Insight, Workspace
from insflow.core.store import Store, get_store, reset_store
from insflow.engine.competitor import CompetitorModule


@pytest.fixture
async def module(tmp_path, monkeypatch):
    import insflow.core.files as files_mod

    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)

    s = Store(db_path=tmp_path / "test.db")
    await s.connect()
    await s.migrate()
    reset_store(s)
    await s.create_workspace(Workspace(id="test-ws", name="Test WS"))

    yield CompetitorModule("test-ws")

    reset_store(None)
    await s.close()


class TestProfile:
    async def test_upsert_and_update(self, module):
        p = await module.upsert_profile("competitor.com", name="竞品一号",
                                        positioning="SaaS 自动化工具",
                                        pricing=[{"tier": "Pro", "price": 49}])
        assert p["name"] == "竞品一号" if False else p["name"] == "竞品一号"

        # 更新
        p2 = await module.upsert_profile("competitor.com", positioning="全栈增长平台")
        assert p2["positioning"] == "全栈增长平台"
        assert len(await module.list_profiles()) == 1

    async def test_multiple_profiles(self, module):
        await module.upsert_profile("a.com", name="A")
        await module.upsert_profile("b.com", name="B")
        profiles = await module.list_profiles()
        assert {p["domain"] for p in profiles} == {"a.com", "b.com"}


class TestKeywordGap:
    async def test_gap_insight_created(self, module):
        ranks = [
            {"keyword": "增长自动化", "position": 8, "volume": 1200, "is_mine": False},
            {"keyword": "竞品对比", "position": 12, "volume": 300, "is_mine": False},
            {"keyword": "小流量词", "position": 15, "volume": 50, "is_mine": False},  # 量太小
            {"keyword": "高排名词", "position": 25, "volume": 900, "is_mine": False},  # 排名差
            {"keyword": "我们已有", "position": 10, "volume": 800, "is_mine": True},
        ]
        result = await module.record_seo_snapshot("competitor.com", ranks)

        assert result["gaps"] == 2  # 增长自动化 + 竞品对比
        assert result["insight_id"]

        store = await get_store()
        insight = await store.get_insight(result["insight_id"])
        assert insight.type == "keyword_gap"
        assert "增长自动化" in insight.summary

    async def test_no_gaps_no_insight(self, module):
        ranks = [{"keyword": "kw", "position": 30, "volume": 50, "is_mine": False}]
        result = await module.record_seo_snapshot("c.com", ranks)
        assert result["gaps"] == 0
        assert result["insight_id"] is None

    async def test_ranks_snapshot_persisted(self, module):
        ranks = [{"keyword": "k1", "position": 3, "volume": 500, "is_mine": True}]
        await module.record_seo_snapshot("c.com", ranks)
        store = await get_store()
        latest = await store.latest_keyword_ranks("test-ws", "c.com")
        assert len(latest) == 1
        assert latest[0]["keyword"] == "k1"


class TestWeeklyReport:
    async def test_report_with_signals(self, module):
        await module.upsert_profile("c.com", name="竞品C", positioning="测试定位")
        await module.record_seo_snapshot("c.com", [
            {"keyword": "热门词", "position": 6, "volume": 2000, "is_mine": False},
        ])

        report = await module.build_weekly_report()
        assert "竞品动向周报" in report
        assert "本周最值得注意的 3 件事" in report
        assert "关键词缺口" in report

    async def test_publish_saves_file(self, module):
        path = await module.publish_weekly_report(["pricing页无变化"])
        assert path
        from insflow.core.files import ReportStore
        reports = ReportStore("test-ws").list_reports("competitors")
        assert len(reports) == 1
