"""测试自定义规则模型 DSL（IM-3）+ 模型效果追踪（IM-5）"""

from datetime import datetime, timedelta, timezone

import pytest

from insflow.core.entities import Action, Feedback, Insight, Metric, Workspace
from insflow.core.store import Store, get_store, reset_store
from insflow.engine.dsl_models import (
    DSLValidationError,
    RuleModel,
    validate_dsl,
)
from insflow.engine.router import ModelContext


VALID_SPEC = {
    "id": "cac-overrun",
    "name": "CAC 超标告警",
    "metric": "cac",
    "condition": {"op": ">=", "value": 500},
    "severity": "high",
    "confidence": 0.85,
    "insight_type": "cac_overrun",
    "title_template": "CAC 达到 {value:.0f}，超过阈值 {threshold}",
    "summary_template": "指标 cac 当前为 {value}，触发条件 >= {threshold}。",
    "actions": [{"action_type": "investigate", "description": "分渠道核算"}],
    "stage_tags": ["S1"],
}


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


class TestDSLValidation:
    def test_valid_spec(self):
        assert validate_dsl(VALID_SPEC) == []

    def test_missing_fields(self):
        errors = validate_dsl({"id": "x"})
        assert any("metric" in e for e in errors)

    def test_bad_operator(self):
        errors = validate_dsl({**VALID_SPEC, "condition": {"op": "like", "value": 1}})
        assert any("op" in e for e in errors)

    def test_bad_severity(self):
        errors = validate_dsl({**VALID_SPEC, "severity": "mega"})
        assert any("severity" in e for e in errors)

    def test_invalid_spec_rejected(self):
        with pytest.raises(DSLValidationError):
            RuleModel({"id": ""})


class TestRuleModel:
    async def test_trigger_on_latest(self):
        model = RuleModel(VALID_SPEC)
        now = datetime.now(timezone.utc)
        ctx = ModelContext(workspace_id="ws", metrics=[
            Metric(workspace_id="ws", entity_type="site", entity_id="m",
                   metric="cac", value=650, ts=now),
        ])
        drafts = await model.evaluate(ctx)
        assert len(drafts) == 1
        assert drafts[0]["type"] == "cac_overrun"
        assert "650" in drafts[0]["title"]
        assert drafts[0]["models_json"] == ["cac-overrun"]

    async def test_no_trigger_below_threshold(self):
        model = RuleModel(VALID_SPEC)
        ctx = ModelContext(workspace_id="ws", metrics=[
            Metric(workspace_id="ws", entity_type="site", entity_id="m",
                   metric="cac", value=200, ts=datetime.now(timezone.utc)),
        ])
        assert await model.evaluate(ctx) == []

    async def test_any_window(self):
        model = RuleModel({**VALID_SPEC, "window": "any"})
        now = datetime.now(timezone.utc)
        ctx = ModelContext(workspace_id="ws", metrics=[
            Metric(workspace_id="ws", entity_type="site", entity_id="m",
                   metric="cac", value=100, ts=now - timedelta(hours=2)),
            Metric(workspace_id="ws", entity_type="site", entity_id="m",
                   metric="cac", value=600, ts=now),
        ])
        # latest 不触发（最新值 600？等等 latest=600 会触发）… any 会触发 1 条（600）
        drafts = await model.evaluate(ctx)
        assert len(drafts) >= 1

    async def test_trend_down(self):
        spec = {**VALID_SPEC, "window": "trend_down_pct", "metric": "traffic",
                "trend_pct": 0.3, "condition": {"op": "<", "value": 10**9}}
        model = RuleModel(spec)
        now = datetime.now(timezone.utc)
        ctx = ModelContext(workspace_id="ws", metrics=[
            Metric(workspace_id="ws", entity_type="site", entity_id="m",
                   metric="traffic", value=1000, ts=now - timedelta(hours=1)),
            Metric(workspace_id="ws", entity_type="site", entity_id="m",
                   metric="traffic", value=600, ts=now),  # -40%
        ])
        drafts = await model.evaluate(ctx)
        assert len(drafts) == 1
        assert drafts[0]["evidence_json"][0]["change_pct"] < 0


class TestModelEffectiveness:
    async def test_hit_rate_aggregation(self, env):
        """insight → action → feedback 链路按模型聚合命中率"""
        s = env["store"]

        ins = await s.create_insight(Insight(
            workspace_id="test-ws", type="cac_overrun",
            title="CAC 超标", summary="x",
            evidence_json=[{"type": "t"}], models_json=["cac-overrun"],
        ))
        action = await s.create_action(Action(
            workspace_id="test-ws", insight_id=ins.id,
            action_type="investigate",
        ))
        await s.create_feedback(Feedback(
            workspace_id="test-ws", action_id=action.id, metric="cac",
            before=650, after=500, delta=-150, verdict="effective",
        ))

        eff = await s.get_model_effectiveness("test-ws")
        assert eff[0]["model_id"] == "cac-overrun"
        assert eff[0]["effective"] == 1
        assert eff[0]["hit_rate"] == 1.0

    async def test_multiple_verdicts(self, env):
        s = env["store"]
        ins = await s.create_insight(Insight(
            workspace_id="test-ws", type="t", title="t", summary="s",
            evidence_json=[{"type": "t"}], models_json=["model-x"],
        ))
        a1 = await s.create_action(Action(
            workspace_id="test-ws", insight_id=ins.id, action_type="t1",
        ))
        a2 = await s.create_action(Action(
            workspace_id="test-ws", insight_id=ins.id, action_type="t2",
        ))
        await s.create_feedback(Feedback(
            workspace_id="test-ws", action_id=a1.id, metric="m",
            before=1, after=2, delta=1, verdict="effective",
        ))
        await s.create_feedback(Feedback(
            workspace_id="test-ws", action_id=a2.id, metric="m",
            before=1, after=1, delta=0, verdict="neutral",
        ))

        eff = await s.get_model_effectiveness("test-ws")
        entry = next(e for e in eff if e["model_id"] == "model-x")
        assert entry["effective"] == 1 and entry["neutral"] == 1
        assert entry["hit_rate"] == 0.5
