"""批次 H：结构自进化 —— 结构提案 / 14 天复盘与准确率 / 行为信号（+ DSL 持久化修复）"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import insflow.core.files as files_mod
from insflow.core.entities import Insight, Workspace
from insflow.core.store import Store, reset_store
from insflow.engine import behavior, evolution
from insflow.engine import structure_evolution as se
from insflow.server.app import app


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)
    monkeypatch.setenv("INSFLOW_MASTER_KEY", "mk")
    monkeypatch.setenv("INSFLOW_DISABLE_SCHEDULER", "1")
    from insflow.core.cache import cache as _cache
    _cache.invalidate("")
    s = Store(db_path=tmp_path / "t.db")
    await s.connect()
    await s.migrate()
    reset_store(s)
    await s.create_workspace(Workspace(id="test-ws", name="T"))
    from insflow.engine.dsl_models import get_dsl_registry
    registry = get_dsl_registry()
    registry._rules.clear()
    yield {"store": s, "tmp": tmp_path}
    registry._rules.clear()
    reset_store(None)
    await s.close()


async def _seed_metric(store, metric: str, values: list[float], *,
                       start: datetime | None = None) -> None:
    t0 = start or (datetime.now(UTC) - timedelta(days=len(values)))
    for i, v in enumerate(values):
        ts = (t0 + timedelta(days=i)).isoformat()
        await store._execute(
            """INSERT INTO metrics (id, workspace_id, entity_type, entity_id,
               metric, value, dim_json, ts, monitor_id, window_key)
               VALUES (?, 'test-ws', 'site', 'main', ?, ?, '{}', ?, 'm', ?)""",
            (f"m-{metric}-{i}", metric, v, ts, f"{ts[:13]}-{i}"))
    await store._db.commit()


async def _seed_playbook(store, *, insight_type="competitor_pricing",
                         action_type="feishu.notify", metric="gsc_clicks",
                         n=4, effective=4, created_at: str = "") -> None:
    for i in range(n):
        await store.add_verification_result(
            "test-ws", action_id=f"a{i}", insight_type=insight_type,
            action_type=action_type, metric=metric,
            verdict="effective" if i < effective else "neutral",
            effect_pct=0.2, significant=i < effective, sample_n=14,
            created_at=created_at or datetime.now(UTC).isoformat())


class TestBehaviorSignals:
    async def test_record_and_top(self, env):
        assert await behavior.record("test-ws", "view", "traffic")
        await behavior.record("test-ws", "view", "traffic")
        await behavior.record("test-ws", "export", "overview")
        rows = await behavior.top("test-ws", "view")
        assert rows[0]["key"] == "traffic" and rows[0]["hits"] == 2
        assert (await behavior.summary("test-ws"))["total_events"] == 3

    async def test_bad_input_is_ignored_not_raised(self, env):
        assert await behavior.record("test-ws", "不存在的类型", "x") is False
        assert await behavior.record("test-ws", "view", "  ") is False
        assert await behavior.record("", "view", "traffic") is False

    async def test_suggest_needs_enough_samples(self, env):
        await behavior.record("test-ws", "view", "traffic")
        weak = await behavior.suggest_defaults("test-ws", min_views=3)
        assert weak["enough"] is False and weak["panel_ids"] == []
        assert "不足" in weak["basis"]

        for _ in range(3):
            await behavior.record("test-ws", "view", "traffic")
            await behavior.record("test-ws", "view", "competitor")
        strong = await behavior.suggest_defaults("test-ws", min_views=3)
        assert strong["enough"] and strong["default_cockpit"] in ("traffic", "competitor")
        assert "traffic.channels" in strong["panel_ids"]

    async def test_board_views_excluded_from_cockpit_suggestion(self, env):
        for _ in range(5):
            await behavior.record("test-ws", "view", "board:auto-frequent")
        out = await behavior.suggest_defaults("test-ws", min_views=3)
        assert out["enough"] is False          # 看板自身的浏览不能反过来推荐看板

    async def test_cockpit_page_records_view(self, env):
        TestClient(app).get("/console/cockpit/overview",
                            params={"workspace_id": "test-ws"})
        rows = await behavior.top("test-ws", "view")
        assert any(r["key"] == "overview" for r in rows)

    async def test_api_signals_and_suggest(self, env):
        await behavior.record("test-ws", "view", "ops")
        cli = TestClient(app)
        assert cli.get("/api/v1/behavior/signals",
                       params={"workspace_id": "test-ws"}).json()["total_events"] == 1
        body = cli.get("/api/v1/behavior/suggest",
                       params={"workspace_id": "test-ws", "min_views": 1}).json()
        assert body["enough"] and body["default_cockpit"] == "ops"


class TestStructureDSLProposal:
    async def test_propose_from_playbook(self, env):
        store = env["store"]
        await _seed_playbook(store, metric="cac")
        await _seed_metric(store, "cac", [100] * 18 + [400, 420])

        runs = await se.propose_dsl_rules("test-ws")
        assert len(runs) == 1 and runs[0]["kind"] == "structure_dsl"

        run = await store.get_evolution_run("test-ws", runs[0]["id"])
        spec = run["after"]["spec"]
        # cac 越高越糟 → 取上尾 p95 报警
        assert spec["metric"] == "cac" and spec["condition"]["op"] == ">="
        assert run["rationale"]["source"] == "playbook_mining"
        assert run["status"] == "proposed"       # 只提案，绝不自动生效

    async def test_lower_is_worse_metric_uses_low_tail(self, env):
        """点击/留存这类"越低越糟"的指标要盯下尾，不能照搬上尾"""
        store = env["store"]
        await _seed_playbook(store, insight_type="retention_decline",
                             metric="retention_rate")
        await _seed_metric(store, "retention_rate", [0.5] * 18 + [0.2, 0.18])
        runs = await se.propose_dsl_rules("test-ws")
        spec = (await store.get_evolution_run("test-ws", runs[0]["id"]))["after"]["spec"]
        assert spec["condition"]["op"] == "<="

    async def test_gate_rejects_noisy_threshold(self, env):
        store = env["store"]
        await _seed_playbook(store, metric="cac")
        await _seed_metric(store, "cac", [100] * 20)   # 常数序列：p95 = 100，规则会天天报
        runs = await se.propose_dsl_rules("test-ws")
        run = await store.get_evolution_run("test-ws", runs[0]["id"])
        gate = await evolution.evaluate_gate("test-ws", run)
        assert gate["passed"] is False
        assert any(c["name"] == "not_too_noisy" and not c["passed"]
                   for c in gate["checks"])

    async def test_apply_then_rollback(self, env):
        store = env["store"]
        await _seed_playbook(store, metric="cac")
        await _seed_metric(store, "cac", [100] * 18 + [400, 420])
        run_id = (await se.propose_dsl_rules("test-ws"))[0]["id"]
        run = await store.get_evolution_run("test-ws", run_id)
        gate = await evolution.evaluate_gate("test-ws", run)
        assert gate["passed"], gate

        res = await evolution.apply_run("test-ws", run_id)
        rule_id = res["ref"]["rule_id"]
        from insflow.engine.dsl_models import get_dsl_registry, persisted_rules
        assert get_dsl_registry().get("test-ws", rule_id) is not None
        assert rule_id in {r["id"] for r in await persisted_rules("test-ws")}

        await evolution.rollback_run("test-ws", run_id)
        assert get_dsl_registry().get("test-ws", rule_id) is None
        assert rule_id not in {r["id"] for r in await persisted_rules("test-ws")}
        assert (await store.get_evolution_run("test-ws", run_id))["status"] == "rolled_back"

    async def test_cooldown_blocks_repeat(self, env):
        store = env["store"]
        await _seed_playbook(store, metric="cac")
        await _seed_metric(store, "cac", [100] * 18 + [400, 420])
        assert len(await se.propose_dsl_rules("test-ws")) == 1
        assert await se.propose_dsl_rules("test-ws") == []


class TestStructureMonitorProposal:
    async def _seed_insights(self, store, n=3, evidence=None):
        for i in range(n):
            await store.create_insight(Insight(
                workspace_id="test-ws", type="competitor_pricing",
                title=f"竞品动作 {i}", summary="s",
                evidence_json=evidence if evidence is not None
                else [{"url": "https://rival.example/pricing"}]))

    async def test_propose_when_no_monitor_covers_it(self, env):
        store = env["store"]
        await self._seed_insights(store)
        runs = await se.propose_monitors("test-ws")
        assert len(runs) == 1
        run = await store.get_evolution_run("test-ws", runs[0]["id"])
        assert run["after"]["kind"] == "site_change"
        assert run["after"]["target"] == {"url": "https://rival.example/pricing"}

    async def test_no_proposal_without_target_in_evidence(self, env):
        await self._seed_insights(env["store"], evidence=[{"note": "没有可监控目标"}])
        assert await se.propose_monitors("test-ws") == []

    async def test_no_proposal_when_kind_already_monitored(self, env):
        store = env["store"]
        await self._seed_insights(store)
        await store.create_monitor("test-ws", "site_change", {"url": "https://x"})
        assert await se.propose_monitors("test-ws") == []

    async def test_apply_creates_monitor_and_rollback_removes(self, env):
        store = env["store"]
        await self._seed_insights(store)
        run_id = (await se.propose_monitors("test-ws"))[0]["id"]
        run = await store.get_evolution_run("test-ws", run_id)
        assert (await evolution.evaluate_gate("test-ws", run))["passed"]

        res = await evolution.apply_run("test-ws", run_id)
        monitor_id = res["ref"]["monitor_id"]
        assert any(m["id"] == monitor_id for m in await store.list_monitors_full("test-ws"))

        await evolution.rollback_run("test-ws", run_id)
        assert not await store.list_monitors_full("test-ws")


class TestStructureBoardProposal:
    async def _seed_views(self):
        for _ in range(4):
            await behavior.record("test-ws", "view", "traffic")
            await behavior.record("test-ws", "view", "competitor")

    async def test_needs_two_panels(self, env):
        for _ in range(4):
            await behavior.record("test-ws", "view", "traffic")
        assert await se.propose_boards("test-ws") == []

    async def test_propose_apply_rollback(self, env):
        store = env["store"]
        await self._seed_views()
        runs = await se.propose_boards("test-ws")
        assert len(runs) == 1
        run = await store.get_evolution_run("test-ws", runs[0]["id"])
        assert (await evolution.evaluate_gate("test-ws", run))["passed"]

        res = await evolution.apply_run("test-ws", run_id := runs[0]["id"])
        from insflow.engine.custom_boards import list_boards
        assert res["ref"]["board_id"] == "auto-frequent"
        assert [b["id"] for b in await list_boards("test-ws")] == ["auto-frequent"]

        await evolution.rollback_run("test-ws", run_id)
        assert await list_boards("test-ws") == []

    async def test_no_proposal_if_identical_board_exists(self, env):
        await self._seed_views()
        from insflow.engine.custom_boards import save_board
        await save_board("test-ws", name="常用", board_id="mine",
                         panel_ids=["traffic.channels", "competitor.all"])
        assert await se.propose_boards("test-ws") == []


class TestProposeAll:
    async def test_includes_structures(self, env):
        store = env["store"]
        await _seed_playbook(store, metric="cac")
        await _seed_metric(store, "cac", [100] * 18 + [400, 420])
        out = await evolution.propose_all("test-ws")
        assert out["structure_dsl"] == 1
        assert set(out) >= {"threshold_tunes", "model_weights", "structure_dsl",
                            "structure_monitor", "structure_board", "runs"}

    async def test_can_be_disabled(self, env):
        store = env["store"]
        await _seed_playbook(store, metric="cac")
        await _seed_metric(store, "cac", [100] * 18 + [400, 420])
        out = await evolution.propose_all("test-ws", structures=False)
        assert out["structure_dsl"] == 0

    async def test_api_structures_endpoint(self, env):
        store = env["store"]
        await _seed_playbook(store, metric="cac")
        await _seed_metric(store, "cac", [100] * 18 + [400, 420])
        body = TestClient(app).post("/api/v1/evolution/structures",
                                    json={"workspace_id": "test-ws"}).json()
        assert body["structure_dsl"] == 1


class TestReviewLoop:
    async def _applied_run(self, store, *, days_ago=20, status="applied") -> dict:
        run = await store.create_evolution_run(
            "test-ws", "model_weight", target="global",
            before={"a": 1.0}, after={"a": 1.2}, rationale={"metric": ""})
        applied = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
        await store.update_evolution_run("test-ws", run["id"], status=status,
                                         applied_at=applied)
        return await store.get_evolution_run("test-ws", run["id"])

    async def test_improved_verdict_written_back(self, env):
        store = env["store"]
        run = await self._applied_run(store)
        applied = datetime.fromisoformat(run["applied_at"])
        # 生效前：3 条里 0 条有效；生效后：4 条全有效
        for i in range(3):
            await store.add_verification_result(
                "test-ws", verdict="neutral",
                created_at=(applied - timedelta(days=3 + i)).isoformat())
        for i in range(4):
            await store.add_verification_result(
                "test-ws", verdict="effective",
                created_at=(applied + timedelta(days=1 + i)).isoformat())

        out = await evolution.review_due("test-ws")
        assert out["reviewed"] == 1 and out["runs"][0]["verdict"] == "improved"

        stored = await store.get_evolution_run("test-ws", run["id"])
        assert stored["reviewed_at"] and stored["review"]["verdict"] == "improved"
        assert stored["review"]["delta_effective_rate"] == 1.0
        assert stored["review"]["north_star"] == "verification_effective_rate"

    async def test_insufficient_samples_not_judged(self, env):
        store = env["store"]
        await self._applied_run(store)
        out = await evolution.review_due("test-ws")
        assert out["runs"][0]["verdict"] == "insufficient"
        acc = await evolution.accuracy("test-ws")
        assert acc["reviewed"] == 1 and acc["judged"] == 0 and acc["accuracy"] is None

    async def test_rolled_back_counts_as_failure(self, env):
        store = env["store"]
        await self._applied_run(store, status="rolled_back")
        await evolution.review_due("test-ws")
        acc = await evolution.accuracy("test-ws")
        assert acc["tally"]["reverted"] == 1
        assert acc["judged"] == 1 and acc["accuracy"] == 0.0

    async def test_not_reviewed_before_window(self, env):
        store = env["store"]
        await self._applied_run(store, days_ago=3)
        assert (await evolution.review_due("test-ws"))["reviewed"] == 0

    async def test_review_is_idempotent(self, env):
        store = env["store"]
        await self._applied_run(store)
        assert (await evolution.review_due("test-ws"))["reviewed"] == 1
        assert (await evolution.review_due("test-ws"))["reviewed"] == 0

    async def test_accuracy_api_and_definition(self, env):
        store = env["store"]
        await self._applied_run(store, status="rolled_back")
        cli = TestClient(app)
        assert cli.post("/api/v1/evolution/review",
                        json={"workspace_id": "test-ws"}).json()["reviewed"] == 1
        body = cli.get("/api/v1/evolution/accuracy",
                       params={"workspace_id": "test-ws"}).json()
        assert body["accuracy"] == 0.0 and "不编数" in body["definition"]

    async def test_console_shows_accuracy_and_behavior(self, env):
        store = env["store"]
        await self._applied_run(store, status="rolled_back")
        await evolution.review_due("test-ws")
        await behavior.record("test-ws", "view", "traffic")
        html = TestClient(app).get("/console/evolution",
                                   params={"workspace_id": "test-ws"}).text
        assert "提案准确率" in html and "行为信号" in html
        assert "只提结构草案" in html


class TestDSLPersistenceFix:
    async def test_saved_rule_survives_registry_reset(self, env):
        from insflow.engine.dsl_models import (
            get_dsl_registry,
            restore_rules,
            save_rule,
        )
        spec = {"id": "r1", "name": "R1", "metric": "gsc_clicks",
                "condition": {"op": ">=", "value": 100}}
        await save_rule("test-ws", spec)
        get_dsl_registry()._rules.clear()          # 模拟进程重启
        assert get_dsl_registry().get("test-ws", "r1") is None
        assert await restore_rules() == 1
        assert get_dsl_registry().get("test-ws", "r1") is not None

    async def test_api_register_and_delete_persist(self, env):
        cli = TestClient(app)
        spec = {"id": "r2", "name": "R2", "metric": "x",
                "condition": {"op": ">=", "value": 1}}
        assert cli.post("/api/v1/models/dsl", params={"workspace_id": "test-ws"},
                        json={"spec": spec}).json()["ok"]
        from insflow.engine.dsl_models import get_dsl_registry, persisted_rules
        assert [r["id"] for r in await persisted_rules("test-ws")] == ["r2"]

        get_dsl_registry()._rules.clear()          # 重启后列表也不该变空
        assert cli.get("/api/v1/models/dsl",
                       params={"workspace_id": "test-ws"}).json()["rules"]

        assert cli.delete("/api/v1/models/dsl/r2",
                          params={"workspace_id": "test-ws"}).json()["ok"]
        assert await persisted_rules("test-ws") == []

    async def test_template_export_includes_dsl_rules(self, env):
        """此前 export 调了不存在的 registry.list() 并被 except 吞掉 → 永远导不出规则"""
        from insflow.engine.dsl_models import save_rule
        from insflow.engine.template_pack import export_from_workspace
        await save_rule("test-ws", {"id": "r3", "name": "R3", "metric": "m",
                                    "condition": {"op": ">=", "value": 2}})
        out = await export_from_workspace("test-ws", template_id="t1")
        assert out["dsl_rules"] == 1
        assert out["spec"]["dsl_rules"][0]["id"] == "r3"
        assert out["errors"] == []
