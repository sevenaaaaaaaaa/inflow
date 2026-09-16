"""测试动作验证状态机（基线记录 + 14 天窗口 + 验证报告）"""

from datetime import datetime, timedelta, timezone

import pytest

from insflow.actions.feedback_tracker import FeedbackTracker, MAX_RETRIES
from insflow.core.entities import Action, ActionState, ActionVerdict, Insight, Workspace
from insflow.core.statemachine import ACTION_MACHINE, InvalidTransition
from insflow.core.store import Store, get_store, reset_store


@pytest.fixture
async def env(tmp_path, monkeypatch):
    """临时环境：store + tracker + workspace + insight + action"""
    import insflow.core.files as files_mod

    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)

    s = Store(db_path=tmp_path / "test.db")
    await s.connect()
    await s.migrate()
    reset_store(s)
    await s.create_workspace(Workspace(id="test-ws", name="Test WS"))

    insight = await s.create_insight(Insight(
        workspace_id="test-ws",
        type="competitor_pricing",
        title="竞品降价",
        summary="测试",
        evidence_json=[{"type": "test"}],
    ))

    action = await s.create_action(Action(
        workspace_id="test-ws",
        insight_id=insight.id,
        action_type="mflow.create_content",
        description="生成应对内容",
    ))

    yield {"store": s, "insight": insight, "action": action, "tracker": FeedbackTracker("test-ws")}

    reset_store(None)
    await s.close()


class TestStateMachineRules:
    async def test_valid_flow(self):
        flow = ["pending", "dispatched", "done", "verifying", "verified"]
        for cur, nxt in zip(flow, flow[1:]):
            ACTION_MACHINE.validate(cur, nxt)

    async def test_invalid_jump(self):
        with pytest.raises(InvalidTransition):
            ACTION_MACHINE.validate("pending", "verifying")


class TestLifecycle:
    async def test_dispatch_records_baseline_and_window(self, env):
        action, tracker = env["action"], env["tracker"]
        baseline = {"gsc_clicks": 100, "ga4_sessions": 3000}

        updated = await tracker.mark_dispatched(action, baseline)

        assert updated.state == ActionState.DISPATCHED
        assert updated.baseline_json == baseline
        assert updated.dispatched_at is not None
        remaining = updated.verify_window_until - datetime.now(timezone.utc)
        assert 13 <= remaining.days <= 14

    async def test_done_enters_verifying(self, env):
        action, tracker = env["action"], env["tracker"]
        await tracker.mark_dispatched(action, {"gsc_clicks": 100})
        store = await get_store()
        updated = await tracker.mark_done(await store.get_action(action.id), {"ref": "mflow:loop:x"})

        assert updated.state == ActionState.VERIFYING
        assert updated.result_json.get("ref") == "mflow:loop:x"

    async def test_failed_retries_then_dead(self, env):
        tracker = env["tracker"]
        store = await get_store()
        action_id = env["action"].id

        for _ in range(MAX_RETRIES - 1):
            state = await tracker.mark_failed(await store.get_action(action_id), "boom")
            assert state.state == ActionState.FAILED

        state = await tracker.mark_failed(await store.get_action(action_id), "boom")
        assert state.state == ActionState.DEAD


class TestEvaluation:
    async def _ready_action(self, env):
        action = env["action"]
        tracker = env["tracker"]
        await tracker.mark_dispatched(action, {"gsc_clicks": 100})
        store = await get_store()
        await tracker.mark_done(await store.get_action(action.id), {})
        return await store.get_action(action.id)

    async def test_effective(self, env):
        action = await self._ready_action(env)
        fb = await env["tracker"].evaluate(action, {"gsc_clicks": 150})

        assert fb.verdict == ActionVerdict.EFFECTIVE
        updated = await (await get_store()).get_action(action.id)
        assert updated.state == ActionState.VERIFIED
        assert updated.result_json["verdict"] == "effective"

    async def test_harmful(self, env):
        action = await self._ready_action(env)
        fb = await env["tracker"].evaluate(action, {"gsc_clicks": 60})
        assert fb.verdict == ActionVerdict.HARMFUL

    async def test_neutral(self, env):
        action = await self._ready_action(env)
        fb = await env["tracker"].evaluate(action, {"gsc_clicks": 105})
        assert fb.verdict == ActionVerdict.NEUTRAL

    async def test_insight_marked_verified(self, env):
        """动作验证通过 → 洞察进入 verified（闭环）"""
        store = env["store"]
        action = await self._ready_action(env)
        await store.update_insight_status(action.insight_id, "actioned")

        await env["tracker"].evaluate(action, {"gsc_clicks": 200})

        insight = await store.get_insight(action.insight_id)
        from insflow.core.entities import InsightStatus
        assert insight.status == InsightStatus.VERIFIED

    async def test_no_baseline_raises(self, env):
        tracker = env["tracker"]
        from insflow.actions.feedback_tracker import VerificationError
        with pytest.raises(VerificationError):
            await tracker.evaluate(env["action"], {})

    async def test_report_saved(self, env):
        action = await self._ready_action(env)
        await env["tracker"].evaluate(action, {"gsc_clicks": 150})

        from insflow.core.files import ReportStore
        reports = ReportStore("test-ws").list_reports("verification")
        assert len(reports) == 1
        assert "验证报告" in reports[0].read_text(encoding="utf-8")

    async def test_evaluate_due_actions(self, env):
        """批量评估到期动作"""
        tracker = env["tracker"]
        action = await self._ready_action(env)
        store = await get_store()

        await store.update_action_state(
            action.id, "verifying",
            verify_window_until=datetime.now(timezone.utc) - timedelta(days=1),
        )

        async def provider(a):
            return {"gsc_clicks": 180}

        results = await tracker.evaluate_due_actions(provider)
        assert len(results) == 1
        assert results[0]["evaluated"] is True
