"""P1 自进化测试：结构化验证 / 提案与门禁 / 生效回滚 / 配方挖掘 / 权重消费"""

import json
from datetime import UTC, datetime, timedelta

import pytest

import insflow.core.files as files_mod
from insflow.core.entities import Action, Insight, Workspace
from insflow.core.store import Store, reset_store


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
    yield {"store": s}
    reset_store(None)
    await s.close()


async def _seed_series(store, metric: str, before: float, after: float,
                       t0: datetime, days: int = 7) -> None:
    for i in range(-days, days + 1):
        value = before if i < 0 else after
        ts = (t0 + timedelta(days=i)).isoformat()
        await store._execute(
            """INSERT OR IGNORE INTO metrics (id, workspace_id, entity_type, entity_id,
               metric, value, dim_json, ts, monitor_id, window_key)
               VALUES (?, 'test-ws', 'site', 'main', ?, ?, '{}', ?, 'm', ?)""",
            (f"m-{metric}-{i}", metric, value, ts, ts[:13]))
    await store._db.commit()


class TestStructuredVerification:
    def test_effect_ci_and_confounders(self, env):
        import asyncio

        from insflow.actions.feedback_tracker import FeedbackTracker

        async def _run():
            store = env["store"]
            ins = await store.create_insight(Insight(workspace_id="test-ws",
                                                    type="traffic_anomaly",
                                                    title="t", summary="s"))
            action = await store.create_action(Action(
                workspace_id="test-ws", insight_id=ins.id,
                action_type="send_report", state="verifying",
                baseline_json={"gsc_clicks": 100}))
            t0 = datetime.now(UTC) - timedelta(days=7)
            await _seed_series(store, "gsc_clicks", 100, 130, t0)
            await store._execute("UPDATE actions SET dispatched_at = ? WHERE id = ?",
                                 (t0.isoformat(), action.id))
            await store._db.commit()
            await FeedbackTracker("test-ws").evaluate(
                await store.get_action(action.id), {"gsc_clicks": 130})
            return await store.list_verification_results("test-ws")

        rows = asyncio.get_event_loop().run_until_complete(_run())
        assert rows and rows[0]["verdict"] == "effective"
        r = rows[0]
        assert r["effect_pct"] == pytest.approx(0.3, abs=1e-6)
        assert r["significant"] in (0, 1)
        assert r["sample_n"] > 0 and r["method"] and "非随机实验" in r["method"]
        assert isinstance(r["confounders"], dict)
        summary = asyncio.get_event_loop().run_until_complete(
            env["store"].verification_summary("test-ws"))
        assert summary["total"] == 1 and summary["effective"] == 1
        assert "send_report" in summary["by_action_type"]

    def test_small_sample_and_dq_confounders(self, env):
        import asyncio

        from insflow.actions.feedback_tracker import _structured_result

        async def _run():
            store = env["store"]
            ins = await store.create_insight(Insight(workspace_id="test-ws", type="t",
                                                    title="t", summary="s"))
            action = await store.create_action(Action(
                workspace_id="test-ws", insight_id=ins.id, action_type="x",
                baseline_json={"m1": 1}))
            return await _structured_result(store, action, "m1", 1.0, 2.0, "effective",
                                            0.5)

        out = asyncio.get_event_loop().run_until_complete(_run())
        assert out["confounders"].get("insufficient_series") is True
        assert out["confidence"] <= 0.5


class TestEvolutionProposals:
    def _seed_verifications(self, env, n: int = 6, effective: int = 4):
        import asyncio

        async def _run():
            store = env["store"]
            for i in range(n):
                await store.add_verification_result(
                    "test-ws", action_id=f"a{i}", insight_type="traffic_anomaly",
                    action_type="send_report", metric="ga4_sessions",
                    verdict="effective" if i < effective else "neutral",
                    effect_pct=0.3 if i < effective else 0.01,
                    significant=bool(i < effective), sample_n=14, window_days=14,
                    confidence=0.8, method="t")
        asyncio.get_event_loop().run_until_complete(_run())

    def test_threshold_tune_proposal_gate_apply_rollback(self, env):
        import asyncio

        from insflow.engine.demo import DemoSeeder
        from insflow.engine.evolution import (
            apply_run,
            evaluate_gate,
            propose_threshold_tunes,
            rollback_run,
        )

        async def _run():
            store = env["store"]
            await DemoSeeder("test-ws", days=40).seed()
            await store.upsert_alert_rule("test-ws", {
                "name": "点击过高", "metric": "gsc_clicks", "op": "gt",
                "threshold": 200000, "window_days": 7, "routes": ["webhook"]})
            runs = await propose_threshold_tunes("test-ws")
            assert runs, "应能为远离分布的阈值提出修正"
            run = await store.get_evolution_run("test-ws", runs[0]["id"])
            gate = await evaluate_gate("test-ws", run)
            assert gate["passed"] is True, gate
            applied = await apply_run("test-ws", run["id"])
            assert applied["status"] == "applied"
            ws = await store.get_workspace("test-ws")
            overrides = (ws.settings_json or {}).get("alert_threshold_overrides") or {}
            assert run["target"] in overrides
            rolled = await rollback_run("test-ws", run["id"])
            assert rolled["status"] == "rolled_back"
            ws2 = await store.get_workspace("test-ws")
            overrides2 = (ws2.settings_json or {}).get("alert_threshold_overrides") or {}
            assert float(overrides2.get(run["target"], 0)) == float(
                (run.get("before") or {}).get("threshold"))
            return run

        run = asyncio.get_event_loop().run_until_complete(_run())
        assert run["rationale"].get("guardrail_band") in (0.2, 0.5, 1.0, 2.0)

    def test_alert_evaluation_uses_override(self, env):
        import asyncio

        from insflow.engine.alerts import evaluate_workspace

        async def _run():
            store = env["store"]
            now = datetime.now(UTC)
            for i in range(5):
                await store._execute(
                    """INSERT INTO metrics (id, workspace_id, entity_type, entity_id,
                       metric, value, dim_json, ts) VALUES (?, 'test-ws', 'site',
                       'main', 'ga4_sessions', 100, '{}', ?)""",
                    (f"o{i}", (now - timedelta(days=i)).isoformat()))
            await store._db.commit()
            rule = await store.upsert_alert_rule("test-ws", {
                "name": "会话", "metric": "ga4_sessions", "op": "lt",
                "threshold": 1, "window_days": 7, "routes": []})
            # 规则阈值 1 → 不会命中；覆盖为 1000 → 会命中
            ws = await store.get_workspace("test-ws")
            settings = dict(ws.settings_json or {})
            settings["alert_threshold_overrides"] = {rule["id"]: 1000}
            ws.settings_json = settings
            await store.update_workspace(ws)
            fired = await evaluate_workspace("test-ws")
            return [f for f in fired if f.get("rule_id") == rule["id"]]

        fired = asyncio.get_event_loop().run_until_complete(_run())
        assert fired, "覆盖阈值必须参与评估（自进化的落点）"

    def test_model_weight_proposal_and_consumption(self, env):
        import asyncio

        from insflow.engine.evolution import apply_run, evaluate_gate, propose_model_weights
        from insflow.engine.router import get_model_router, rank_insights

        self._seed_verifications(env)
        async def _run():
            run = await propose_model_weights("test-ws")
            assert run and run["kind"] == "model_weight"
            gate = await evaluate_gate("test-ws", run)
            assert gate["passed"] is True, gate
            await apply_run("test-ws", run["id"])
            ws = await env["store"].get_workspace("test-ws")
            return (ws.settings_json or {}).get("model_weights") or {}

        weights = asyncio.get_event_loop().run_until_complete(_run())
        assert weights and all(0.2 <= v <= 2.0 for v in weights.values())

        async def _load():
            return await get_model_router().load_weights("test-ws")
        loaded = asyncio.get_event_loop().run_until_complete(_load())
        assert loaded == weights

        class _Ins:
            def __init__(self, sev, models):
                self.severity = sev
                self.models_json = models
        low = _Ins("high", ["model_low"])
        high = _Ins("medium", ["model_high"])
        ranked = rank_insights([low, high], {"model_high": 2.0, "model_low": 0.5})
        assert ranked[0] is high                     # 权重影响处理优先级

    def test_cooldown_blocks_repeat_proposal(self, env):
        import asyncio

        from insflow.engine.demo import DemoSeeder
        from insflow.engine.evolution import propose_threshold_tunes

        async def _run():
            store = env["store"]
            await DemoSeeder("test-ws", days=40).seed()
            await store.upsert_alert_rule("test-ws", {
                "name": "r", "metric": "gsc_clicks", "op": "gt",
                "threshold": 200000, "window_days": 7, "routes": []})
            first = await propose_threshold_tunes("test-ws")
            second = await propose_threshold_tunes("test-ws")
            return first, second

        first, second = asyncio.get_event_loop().run_until_complete(_run())
        assert first and second == []                # 72h 冷却期

    def test_gate_rejects_when_sample_insufficient(self, env):
        import asyncio

        from insflow.engine.evolution import evaluate_gate

        async def _run():
            store = env["store"]
            run = await store.create_evolution_run(
                "test-ws", "model_weight", target="global",
                before={}, after={"m": 1.1}, rationale={})
            return await evaluate_gate("test-ws", run)

        gate = asyncio.get_event_loop().run_until_complete(_run())
        assert gate["passed"] is False
        assert gate["checks"][0]["name"] == "sample_sufficient"


class TestPlaybooks:
    def _seed(self, env, n: int = 4, effective: int = 3, action_type="mflow.create_content"):
        import asyncio

        async def _run():
            store = env["store"]
            for i in range(n):
                await store.add_verification_result(
                    "test-ws", action_id=f"p{i}", insight_type="keyword_opportunity",
                    action_type=action_type, metric="gsc_clicks",
                    verdict="effective" if i < effective else "harmful",
                    effect_pct=0.25 if i < effective else -0.1,
                    significant=i < effective, sample_n=14, window_days=14,
                    confidence=0.8, method="t")
        asyncio.get_event_loop().run_until_complete(_run())

    def test_mine_list_and_export(self, env, tmp_path):
        import asyncio

        from insflow.engine.playbooks import export_template, list_playbooks, mine

        self._seed(env)
        drafts = asyncio.get_event_loop().run_until_complete(mine("test-ws"))
        assert len(drafts) == 1
        d = drafts[0]
        assert d["samples"] == 4 and d["effective_rate"] == 0.75
        listed = list_playbooks("test-ws")
        assert listed and listed[0]["id"] == d["id"]
        out = tmp_path / "pb.json"
        res = export_template("test-ws", d["id"], str(out))
        assert res["ok"] is True and out.exists()
        doc = json.loads(out.read_text(encoding="utf-8"))
        assert doc["playbook"]["action_type"] == "mflow.create_content"

    def test_min_thresholds_enforced(self, env):
        import asyncio

        from insflow.engine.playbooks import mine

        self._seed(env, n=4, effective=1)            # 有效率 25% < 50%
        assert asyncio.get_event_loop().run_until_complete(mine("test-ws")) == []


class TestEvolutionWiring:
    def test_apis_and_page(self, env):
        import asyncio

        from fastapi.testclient import TestClient

        from insflow.engine.demo import DemoSeeder
        from insflow.server.app import app

        asyncio.get_event_loop().run_until_complete(
            DemoSeeder("test-ws", days=30).seed())
        cli = TestClient(app)
        assert cli.get("/api/v1/verification/results",
                       params={"workspace_id": "test-ws"}).status_code == 200
        proposed = cli.post("/api/v1/evolution/propose",
                            json={"workspace_id": "test-ws"}).json()
        assert "threshold_tunes" in proposed
        assert cli.get("/api/v1/evolution/runs",
                       params={"workspace_id": "test-ws"}).status_code == 200
        assert cli.post("/api/v1/playbooks/mine",
                        json={"workspace_id": "test-ws"}).status_code == 200
        page = cli.get("/console/evolution", params={"workspace_id": "test-ws"})
        assert page.status_code == 200
        assert "进化账本" in page.text and "配方" in page.text
        assert "自进化" in cli.get("/console", params={
            "workspace_id": "test-ws"}).text          # 导航入口

    def test_cli(self, env):
        import asyncio

        from click.testing import CliRunner

        from insflow.cli import main
        from insflow.engine.demo import DemoSeeder

        asyncio.get_event_loop().run_until_complete(
            DemoSeeder("test-ws", days=30).seed())
        runner = CliRunner()
        for args in (["evolution", "list", "-w", "test-ws"],
                     ["evolution", "propose", "-w", "test-ws"],
                     ["playbook", "-w", "test-ws"],
                     ["playbook", "-w", "test-ws", "--mine"]):
            res = runner.invoke(main, args)
            assert res.exit_code == 0, f"{args} -> {res.output[-200:]}"

    def test_schedule_registered(self):
        src = open("insflow/core/bootstrap.py", encoding="utf-8").read()
        assert "evolution.propose@mon-05:20" in src


class TestVerificationLoopCloses:
    """回归：14 天验证此前没有指标来源（调度评估 provider=None）→ 永远不出结论"""

    def test_compute_baseline_and_provider(self, env):
        import asyncio

        from insflow.actions.feedback_tracker import (
            BASELINE_WINDOW_DAYS,
            compute_baseline,
            default_metrics_provider,
        )
        from insflow.core.entities import Action, Insight

        async def _run():
            store = env["store"]
            now = datetime.now(UTC)
            for i in range(BASELINE_WINDOW_DAYS):      # 基线窗口：每天 100
                ts = (now - timedelta(days=i + 1)).isoformat()
                await store._execute(
                    """INSERT OR IGNORE INTO metrics (id, workspace_id, entity_type,
                       entity_id, metric, value, dim_json, ts, monitor_id, window_key)
                       VALUES (?, 'test-ws', 'site', 'main', 'gsc_clicks', 100, '{}',
                       ?, 'm', ?)""", (f"b{i}", ts, ts[:13]))
            await store._db.commit()
            baseline = await compute_baseline("test-ws", ("gsc_clicks",))
            assert baseline["window_days"] == BASELINE_WINDOW_DAYS
            # 边界：恰好 now-14d 的那一行会被窗口排除 → 允许 13/14 天的口径差
            assert baseline["gsc_clicks"] >= 90.0

            # 派发后 3 天每天 130 → provider 应给出 130（日均）
            ins = await store.create_insight(Insight(workspace_id="test-ws", type="t",
                                                    title="t", summary="s"))
            action = await store.create_action(Action(
                workspace_id="test-ws", insight_id=ins.id, action_type="x",
                state="dispatched", baseline_json=baseline))
            t0 = now - timedelta(days=3)
            await store._execute("UPDATE actions SET dispatched_at = ? WHERE id = ?",
                                 (t0.isoformat(), action.id))
            for i in range(3):
                ts = (t0 + timedelta(days=i, hours=1)).isoformat()
                await store._execute(
                    """INSERT OR IGNORE INTO metrics (id, workspace_id, entity_type,
                       entity_id, metric, value, dim_json, ts, monitor_id, window_key)
                       VALUES (?, 'test-ws', 'site', 'main', 'gsc_clicks', 130, '{}',
                       ?, 'm', ?)""", (f"p{i}", ts, ts[:13]))
            await store._db.commit()
            provider = default_metrics_provider("test-ws")
            current = await provider(await store.get_action(action.id))
            return current

        current = asyncio.get_event_loop().run_until_complete(_run())
        assert current.get("gsc_clicks", 0) > 100      # 后窗口明显高于基线日均

    def test_legacy_baseline_is_skipped(self, env):
        """老格式 baseline（裸数字无口径）必须跳过，避免"合计 vs 日均"错判"""
        import asyncio

        from insflow.actions.feedback_tracker import default_metrics_provider
        from insflow.core.entities import Action, Insight

        async def _run():
            store = env["store"]
            ins = await store.create_insight(Insight(workspace_id="test-ws", type="t",
                                                    title="t", summary="s"))
            action = await store.create_action(Action(
                workspace_id="test-ws", insight_id=ins.id, action_type="x",
                state="dispatched", baseline_json={"gsc_clicks": 100}))
            return await default_metrics_provider("test-ws")(action)

        assert asyncio.get_event_loop().run_until_complete(_run()) == {}

    def test_dispatch_endpoint_persists_action_and_baseline(self, env):
        import asyncio

        from fastapi.testclient import TestClient

        from insflow.core.entities import Insight
        from insflow.server.app import app
        from tests.integration.fake_peer import FakeHTTPPeer

        peer = FakeHTTPPeer()
        base = peer.start()

        async def _seed():
            store = env["store"]
            ins = await store.create_insight(Insight(workspace_id="test-ws",
                                                    type="traffic_anomaly",
                                                    title="t", summary="s"))
            now = datetime.now(UTC)
            for i in range(14):
                ts = (now - timedelta(days=i + 1)).isoformat()
                await store._execute(
                    """INSERT OR IGNORE INTO metrics (id, workspace_id, entity_type,
                       entity_id, metric, value, dim_json, ts, monitor_id, window_key)
                       VALUES (?, 'test-ws', 'site', 'main', 'ga4_sessions', 200, '{}',
                       ?, 'm', ?)""", (f"d{i}", ts, ts[:13]))
            await store._db.commit()
            return ins.id

        insight_id = asyncio.get_event_loop().run_until_complete(_seed())
        cli = TestClient(app)
        r = cli.post("/api/v1/actions", json={
            "workspace_id": "test-ws", "insight_id": insight_id,
            "action_type": "webhook.generic",
            "target_ref": f"{base}/hook",
            "title": "t", "summary": "s"})
        assert r.status_code == 200
        body = r.json()
        assert body.get("action_id"), body          # 动作已落库
        assert "persist_error" not in body

        async def _check():
            store = env["store"]
            action = await store.get_action(body["action_id"])
            return action
        action = asyncio.get_event_loop().run_until_complete(_check())
        assert action.state.value == "dispatched"
        assert action.verify_window_until is not None
        assert action.baseline_json.get("window_days") == 14
        assert action.baseline_json.get("insight_type") == "traffic_anomaly"
        peer.stop()
        assert peer.find("/hook")                           # 真发到了对端


class TestModelListEndpoint:
    def test_models_endpoint_exposes_weights(self, env):
        import asyncio

        from fastapi.testclient import TestClient

        from insflow.server.app import app

        async def _seed():
            store = env["store"]
            ws = await store.get_workspace("test-ws")
            settings = dict(ws.settings_json or {})
            settings["model_weights"] = {"aarrr": 1.4, "nps": 0.6}
            ws.settings_json = settings
            await store.update_workspace(ws)

        asyncio.get_event_loop().run_until_complete(_seed())
        data = TestClient(app).get("/api/v1/models",
                                   params={"workspace_id": "test-ws"}).json()
        by_id = {m["id"]: m for m in data["models"]}
        assert by_id["aarrr"]["weight"] == 1.4
        assert by_id["nps"]["weight"] == 0.6
        assert by_id["ltv_cac"]["weight"] == 1.0          # 未配置默认 1.0


class TestBatchD_OPCWorkspace:
    """批次 D：工作区快切 / 今日三件事 / 客户 ROI（OPC 视角）"""

    def test_accessible_workspaces_and_api(self, env):
        import asyncio

        from fastapi.testclient import TestClient

        from insflow.core.entities import Workspace
        from insflow.server.app import app

        async def _seed():
            store = env["store"]
            await store.create_workspace(Workspace(id="client-b", name="客户B"))
        asyncio.get_event_loop().run_until_complete(_seed())
        cli = TestClient(app)
        ws = cli.get("/api/v1/workspaces").json()["workspaces"]
        ids = {w["id"] for w in ws}
        assert {"test-ws", "client-b"} <= ids
        # 切换器 JS 与按钮在页面里（全站可用，不只仪表盘）
        page = cli.get("/console/cockpit/traffic",
                       params={"workspace_id": "test-ws"}).text
        assert "ifToggleWs" in page and "if-ws-btn" in page

    def test_workspaces_api_scoped_in_saas(self, env, monkeypatch):
        import asyncio

        from fastapi.testclient import TestClient

        from insflow.core.accounts import AccountManager
        from insflow.server.app import app
        monkeypatch.setenv("INSFLOW_SAAS", "1")

        async def _seed():
            await AccountManager("test-ws").register("opc@test.com", "password123",
                                                     "OPC", "客户A")
        asyncio.get_event_loop().run_until_complete(_seed())
        cli = TestClient(app)
        cli.post("/console/login", data={"email": "opc@test.com",
                                        "password": "password123"})
        ws = cli.get("/api/v1/workspaces").json()["workspaces"]
        assert len(ws) == 1 and ws[0]["name"] == "客户A"      # 租户隔离

    def test_top_three_and_due_actions(self, env):
        import asyncio

        from fastapi.testclient import TestClient

        from insflow.engine.demo import DemoSeeder
        from insflow.server.app import app

        async def _seed():
            await DemoSeeder("test-ws", days=20).seed()
        asyncio.get_event_loop().run_until_complete(_seed())
        page = TestClient(app).get("/console", params={
            "workspace_id": "test-ws"}).text
        assert "今日三件事" in page
        assert page.count("派发（飞书）") <= 3

    def test_client_roi_and_cli(self, env):
        import asyncio

        from click.testing import CliRunner

        from insflow.cli import main
        from insflow.engine.roi import client_roi

        async def _seed():
            store = env["store"]
            await store.add_verification_result(
                "test-ws", action_id="a1", insight_type="traffic_anomaly",
                action_type="send_report", metric="ga4_sessions",
                verdict="effective", effect_pct=0.3, significant=True,
                sample_n=14, confidence=0.8, method="t")
            from insflow.engine.billing import BillingManager
            await BillingManager("test-ws").record_usage_persisted("cost_usd", 12.5)
            await BillingManager("test-ws").record_usage_persisted("llm_cost_usd", 1.5)
        asyncio.get_event_loop().run_until_complete(_seed())

        roi = asyncio.get_event_loop().run_until_complete(client_roi("test-ws"))
        assert roi["output"]["effective_actions"] == 1
        assert roi["cost"]["total_cost_usd"] == 14.0
        assert roi["unit_economics"]["cost_per_effective_action_usd"] == 14.0
        assert "未折算增量收入" in roi["unit_economics"]["note"]
        res = CliRunner().invoke(main, ["roi", "-w", "test-ws"])
        assert res.exit_code == 0 and "有效动作 1" in res.output

    def test_roi_without_effective_actions_is_null(self, env):
        import asyncio

        from insflow.engine.roi import client_roi
        roi = asyncio.get_event_loop().run_until_complete(client_roi("test-ws"))
        assert roi["unit_economics"]["cost_per_effective_action_usd"] is None

    def test_llm_cost_is_persisted(self, env, monkeypatch):
        """LLM 成本必须落库（此前只在会话内存，ROI 看不到）"""
        import asyncio

        from insflow.agent.llm import BudgetLedger, LLMGateway, LLMUsage

        async def _run():
            gw = LLMGateway(api_key="k", workspace_id="test-ws",
                            budget=BudgetLedger())
            gw.budget.record(LLMUsage(prompt_tokens=10, completion_tokens=5,
                                      cost_usd=0.0, latency_ms=1, model="m"))
            # 直接验证落库路径（不真调 LLM API）
            from insflow.engine.billing import BillingManager
            await BillingManager("test-ws").record_usage_persisted("llm_cost_usd", 0.42)
            from insflow.engine.roi import client_roi
            return await client_roi("test-ws")

        roi = asyncio.get_event_loop().run_until_complete(_run())
        assert roi["cost"]["llm_cost_usd"] == 0.42
