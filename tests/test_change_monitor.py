"""测试网站变更监控（Firecrawl 抓取 + 语义 diff + 洞察生成）"""

import pytest

from insflow.engine.change_monitor import ChangeMonitor, extract_prices


@pytest.fixture
async def monitor(tmp_path, monkeypatch):
    """变更监控（数据目录与全局 store 重定向到临时目录）"""
    import insflow.core.files as files_mod
    from insflow.core.store import Store, reset_store
    from insflow.core.entities import Workspace

    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)

    s = Store(db_path=tmp_path / "test.db")
    await s.connect()
    await s.migrate()
    reset_store(s)
    await s.create_workspace(Workspace(id="test-ws", name="Test WS"))

    yield ChangeMonitor("test-ws")

    reset_store(None)
    await s.close()


class TestPriceExtraction:
    async def test_extract_prices(self):
        md = "- 基础版 $49/月\n- 专业版 $99/月\n- 企业版 $1,299.00"
        prices = extract_prices(md)
        assert prices == [49, 99, 1299.00]

    async def test_cny(self):
        md = "年费 ¥499"
        assert extract_prices(md) == [499]

    async def test_no_prices(self):
        assert extract_prices("纯文本没有价格") == []


class TestSemanticDiff:
    async def test_price_change_detected(self, monitor):
        old = "基础版 $49/月\n专业版 $99/月"
        new = "基础版 $39/月\n专业版 $99/月"
        diff = monitor.semantic_diff(old, new)
        assert diff["significant"]
        assert len(diff["price_changes"]) == 1
        assert diff["price_changes"][0]["pct"] < 0  # 降价

    async def test_no_change(self, monitor):
        md = "Same content $49"
        diff = monitor.semantic_diff(md, md)
        assert not diff["significant"]
        assert diff["changed_ratio"] == 0

    async def test_content_change(self, monitor):
        old = "# 产品 A\n功能列表1\n功能列表2"
        new = "# 产品 A\n功能列表1\n功能列表2\n全新模块X\n全新模块Y\n全新模块Z"
        diff = monitor.semantic_diff(old, new)
        assert diff["significant"]


class TestChangeCheck:
    @pytest.mark.asyncio
    async def test_first_run_no_insight(self, monitor):
        """首次运行只存快照，不出洞察"""
        result = await monitor.check("mon-1", "https://competitor.com/pricing", "# Pricing $49")
        assert result["first_run"]
        assert not result["significant"]
        assert "insight_id" not in result or result["insight_id"] is None

    @pytest.mark.asyncio
    async def test_price_drop_creates_insight(self, monitor):
        """价格下降 → 产出定价异动洞察"""
        await monitor.check("mon-1", "https://competitor.com/pricing", "基础版 $49/月")
        result = await monitor.check("mon-1", "https://competitor.com/pricing", "基础版 $29/月")
        assert result["significant"]
        assert result["insight_id"]

        from insflow.core.store import get_store
        store = await get_store()
        insight = await store.get_insight(result["insight_id"])
        assert insight.type == "competitor_pricing"
        assert "降价" in insight.title

    @pytest.mark.asyncio
    async def test_same_day_overwrite_snapshot(self, monitor):
        """同日多次运行覆盖快照（不会把同日内容当 diff）"""
        await monitor.check("mon-2", "https://a.com", "content v1")
        r1 = await monitor.check("mon-2", "https://a.com", "content v2")
        # 同日第二次运行：与今日快照 diff
        assert r1["first_run"] is False
