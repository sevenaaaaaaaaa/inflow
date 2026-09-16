"""测试内置模型库 v1（8 个模型）"""

from datetime import UTC, datetime, timedelta

from insflow.core.entities import Metric
from insflow.engine.models.growth_models import (
    JourneyGapModel,
    KeywordOpportunityModel,
    NPSModel,
    RetentionHealthModel,
)
from insflow.engine.models.ltv_cac import LTCACModel
from insflow.engine.router import ModelContext, get_model_router


def _metric(metric, value, entity="main", dim=None, ts=None):
    return Metric(
        workspace_id="ws", entity_type="site", entity_id=entity,
        metric=metric, value=float(value), dim_json=dim or {},
        ts=ts or datetime.now(UTC),
    )


class TestRegistry:
    def test_8_builtin_models_registered(self):
        models = get_model_router().list_models()
        ids = {m["id"] for m in models}
        assert {
            "aarrr", "competitor_momentum", "ltv_cac", "keyword_opportunity",
            "retention_health", "pricing_watch", "nps", "journey_gap",
        } == ids


class TestLTCAC:
    async def test_unhealthy(self):
        now = datetime.now(UTC)
        ctx = ModelContext(workspace_id="ws", metrics=[
            _metric("ltv", 900, ts=now - timedelta(hours=1)),
            _metric("cac", 400, ts=now),
        ])
        insights = await LTCACModel().evaluate(ctx)
        assert len(insights) == 1
        assert insights[0]["severity"] == "medium"  # 2.25:1

    async def test_opportunity(self):
        now = datetime.now(UTC)
        ctx = ModelContext(workspace_id="ws", metrics=[
            _metric("ltv", 3000, ts=now - timedelta(hours=1)),
            _metric("cac", 400, ts=now),
        ])
        insights = await LTCACModel().evaluate(ctx)
        assert insights[0]["type"] == "ltv_cac_opportunity"  # 7.5:1

    async def test_healthy_no_insight(self):
        now = datetime.now(UTC)
        ctx = ModelContext(workspace_id="ws", metrics=[
            _metric("ltv", 1500, ts=now),  # 3.75:1 → 无洞察
            _metric("cac", 400, ts=now),
        ])
        assert await LTCACModel().evaluate(ctx) == []


class TestKeywordOpportunity:
    async def test_opportunity_detected(self):
        now = datetime.now(UTC)
        ctx = ModelContext(workspace_id="ws", metrics=[
            _metric("gsc_impressions", 800, dim={
                "key": "增长自动化工具", "position": 8, "ctr": 0.02,
            }, ts=now),
        ])
        insights = await KeywordOpportunityModel().evaluate(ctx)
        assert len(insights) == 1
        assert "增长自动化工具" in insights[0]["title"]

    async def test_top_rank_skipped(self):
        now = datetime.now(UTC)
        ctx = ModelContext(workspace_id="ws", metrics=[
            _metric("gsc_impressions", 800, dim={
                "key": "kw", "position": 2, "ctr": 0.15,
            }, ts=now),
        ])
        assert await KeywordOpportunityModel().evaluate(ctx) == []


class TestRetention:
    async def test_decline(self):
        now = datetime.now(UTC)
        ctx = ModelContext(workspace_id="ws", metrics=[
            _metric("ga4_retention", 0.50, ts=now - timedelta(days=4)),
            _metric("ga4_retention", 0.44, ts=now - timedelta(days=3)),
            _metric("ga4_retention", 0.40, ts=now - timedelta(days=2)),
            _metric("ga4_retention", 0.35, ts=now - timedelta(days=1)),
        ])
        insights = await RetentionHealthModel().evaluate(ctx)
        assert insights[0]["severity"] == "high"

    async def test_stable_ok(self):
        now = datetime.now(UTC)
        ctx = ModelContext(workspace_id="ws", metrics=[
            _metric("ga4_retention", 0.45, ts=now - timedelta(days=i)) for i in range(3, 0, -1)
        ])
        assert await RetentionHealthModel().evaluate(ctx) == []


class TestNPS:
    async def test_big_drop(self):
        now = datetime.now(UTC)
        ctx = ModelContext(workspace_id="ws", metrics=[
            _metric("nps_score", 45, ts=now - timedelta(days=7)),
            _metric("nps_score", 25, ts=now),
        ])
        insights = await NPSModel().evaluate(ctx)
        assert insights[0]["type"] == "nps_shift"

    async def test_negative(self):
        now = datetime.now(UTC)
        ctx = ModelContext(workspace_id="ws", metrics=[
            _metric("nps_score", -5, ts=now),
        ])
        insights = await NPSModel().evaluate(ctx)
        assert insights[0]["type"] == "nps_negative"


class TestJourneyGap:
    async def test_funnel_drop(self):
        now = datetime.now(UTC)
        ctx = ModelContext(workspace_id="ws", metrics=[
            _metric("journey_step", 1000, dim={"step_name": "visit_pricing", "step": 1000},
                    ts=now - timedelta(hours=2)),
            _metric("journey_step", 200, dim={"step_name": "checkout_started", "step": 200}, ts=now),
        ])
        insights = await JourneyGapModel().evaluate(ctx)
        assert len(insights) == 1
        assert insights[0]["evidence_json"][0]["conversion"] == 0.2
        assert "visit_pricing" in insights[0]["title"]
