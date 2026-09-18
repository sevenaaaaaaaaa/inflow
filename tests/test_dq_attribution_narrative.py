"""Batch10 测试：数据质量 SLA / 归因与增量 / 自动叙事与问数"""

import json
from datetime import UTC, datetime, timedelta

import pytest

import insflow.core.files as files_mod
from insflow.core.entities import Workspace
from insflow.core.store import Store, reset_store
from insflow.engine import attribution as attr
from insflow.engine import data_quality as dq
from insflow.engine import narrative as nar


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


async def _add_metric(store, metric: str, ts: str, value: float = 1.0, entity: str = "main"):
    await store._execute(
        """INSERT INTO metrics (id, workspace_id, entity_type, entity_id, metric, value,
           dim_json, ts) VALUES (?, 'test-ws', 'site', ?, ?, ?, '{}', ?)""",
        (f"{metric}-{ts}-{value}", entity, metric, value, ts))
    await store._db.commit()


class TestDataQuality:
    def test_freshness_statuses(self, env):
        import asyncio

        async def _run():
            store = env["store"]
            now = datetime.now(UTC)
            await _add_metric(store, "fresh_m", (now - timedelta(hours=2)).isoformat())
            await _add_metric(store, "late_m", (now - timedelta(hours=30)).isoformat())
            await _add_metric(store, "stale_m", (now - timedelta(hours=100)).isoformat())
            return await dq.check_workspace("test-ws", window_days=30)

        rep = asyncio.get_event_loop().run_until_complete(_run())
        status = {i["metric"]: i["status"] for i in rep["items"]}
        assert status["fresh_m"] == "fresh"          # < 26h
        assert status["late_m"] == "late"            # 26~52h
        assert status["stale_m"] == "stale"          # > 52h
        assert rep["summary"]["stale"] >= 1

    def test_stale_metric_outside_window_is_detected(self, env):
        import asyncio

        async def _run():
            store = env["store"]
            # 数据远早于窗口 → 靠「全量最近一次」查询也必须被发现（回归）
            await _add_metric(store, "dead_metric", "2026-01-01T00:00:00+00:00")
            return await dq.check_workspace("test-ws", window_days=7,
                                            staleness_only=True)

        rep = asyncio.get_event_loop().run_until_complete(_run())
        assert any(i["metric"] == "dead_metric" and i["status"] == "stale"
                   for i in rep["items"])

    def test_gaps_exclude_today(self, env):
        import asyncio

        async def _run():
            store = env["store"]
            now = datetime.now(UTC)
            # 昨天有、前天缺 → 缺口只应包含前天（今天未结束不算缺口）
            await _add_metric(store, "gappy", (now - timedelta(days=1)).isoformat())
            return await dq.detect_gaps("test-ws", "gappy", days=4)

        gaps = asyncio.get_event_loop().run_until_complete(_run())
        today = datetime.now(UTC).date()
        assert str(today - timedelta(days=1)) not in gaps
        assert str(today - timedelta(days=2)) in gaps
        assert str(today) not in gaps

    def test_sla_override_from_settings(self, env):
        import asyncio

        async def _run():
            store = env["store"]
            ws = await store.get_workspace("test-ws")
            settings = dict(ws.settings_json or {})
            settings["dq_sla"] = {"tight_m": 1.0}
            ws.settings_json = settings
            await store.update_workspace(ws)
            await _add_metric(store, "tight_m",
                              (datetime.now(UTC) - timedelta(hours=3)).isoformat())
            return await dq.check_workspace("test-ws", window_days=7)

        rep = asyncio.get_event_loop().run_until_complete(_run())
        item = next(i for i in rep["items"] if i["metric"] == "tight_m")
        assert item["sla_hours"] == 1.0 and item["status"] == "stale"

    def test_backfill_dry_run_and_push_required(self, env):
        import asyncio

        async def _run():
            store = env["store"]
            now = datetime.now(UTC)
            await _add_metric(store, "gsc_clicks", (now - timedelta(days=1)).isoformat())
            return await dq.backfill("test-ws", days=4, dry_run=True)

        res = asyncio.get_event_loop().run_until_complete(_run())
        assert res["dry_run"] is True and res["ran"] == []
        # 无匹配监控 → 标记为需推送方重推（不能假装能自动续采）
        assert all(s["reason"] == "push_required" for s in res["skipped"]) or res["planned"]

    def test_backfill_maps_monitor(self, env):
        import asyncio

        async def _run():
            store = env["store"]
            await store.create_monitor("test-ws", "keyword", {}, "0 */6 * * *")
            now = datetime.now(UTC)
            await _add_metric(store, "gsc_clicks", (now - timedelta(days=1)).isoformat())
            return await dq.backfill("test-ws", days=4, dry_run=True)

        res = asyncio.get_event_loop().run_until_complete(_run())
        assert any(p["metric"] == "gsc_clicks" for p in res["planned"])

    def test_alert_stale_notifies(self, env, monkeypatch):
        import asyncio

        calls = []

        async def fake_notify(ws, title, summary, routes, escalation):
            calls.append((ws, title, summary))
            return {"ok": True}
        monkeypatch.setattr("insflow.engine.alerts._notify", fake_notify)

        async def _run():
            store = env["store"]
            await _add_metric(store, "stale_x",
                              (datetime.now(UTC) - timedelta(hours=200)).isoformat())
            return await dq.alert_stale("test-ws")

        out = asyncio.get_event_loop().run_until_complete(_run())
        assert out["fired"] >= 1 and calls and "数据质量告警" in calls[0][1]
        assert json.dumps(out, default=str)          # 可序列化（API 返回）


class TestAttribution:
    def test_credit_methods_sum_to_one(self):
        seq = ["search", "social", "direct"]
        for method in attr.METHODS:
            credit = attr._credit_sequence(seq, method)
            assert credit and abs(sum(credit.values()) - 1.0) < 1e-6, method
        assert attr._credit_sequence(seq, "last_click") == {"direct": 1.0}
        assert attr._credit_sequence(seq, "first_click") == {"search": 1.0}
        assert attr._credit_sequence(seq, "linear") == {"search": 1 / 3, "social": 1 / 3,
                                                        "direct": 1 / 3}
        with pytest.raises(attr.AttributionError):
            attr._credit_sequence(seq, "bogus")

    def test_time_decay_favours_later_touch(self):
        credit = attr._credit_sequence(["a", "b", "c"], "time_decay")
        assert credit["c"] > credit["b"] > credit["a"]

    def test_channel_credit_from_events(self, env):
        import asyncio

        async def _run():
            store = env["store"]
            for i in range(10):
                for ch in ("search", "social", "direct"):
                    await store.save_journey_event("test-ws", identity=f"u{i}",
                                                   stage="visit", event="touch",
                                                   props={"channel": ch})
            return await attr.channel_credit("test-ws", days=30, method="last_click")

        res = asyncio.get_event_loop().run_until_complete(_run())
        assert res["degraded"] is False and res["paths_used"] == 10
        assert res["channels"][0]["channel"] == "direct"
        assert any("非随机实验" in n for n in res["notes"])

    def test_channel_credit_degrades_without_paths(self, env):
        import asyncio

        async def _run():
            store = env["store"]
            for i, ch in enumerate(("search", "social")):
                for j in range(3):
                    await store._execute(
                        """INSERT INTO metrics (id, workspace_id, entity_type, entity_id,
                           metric, value, dim_json, ts) VALUES (?, 'test-ws', 'site',
                           'main', 'ga4_conversions', ?, ?, ?)""",
                        (f"c{i}{j}", 10 * (i + 1), json.dumps({"channel": ch}),
                         "2026-09-10T00:00:00+00:00"))
            await store._db.commit()
            return await attr.channel_credit("test-ws", days=3650)

        res = asyncio.get_event_loop().run_until_complete(_run())
        assert res["degraded"] is True
        assert {c["channel"] for c in res["channels"]} == {"search", "social"}
        assert any("退化" in n for n in res["notes"])

    def test_action_lift_pre_post(self, env):
        import asyncio
        from insflow.core.entities import Action

        async def _run():
            store = env["store"]
            from insflow.core.entities import Insight
            ins = await store.create_insight(Insight(
                workspace_id="test-ws", type="t", title="t", summary="s"))
            t0 = datetime.now(UTC) - timedelta(days=5)
            for d in range(12):                       # 前 6 天低、后 6 天高
                ts = (t0 - timedelta(days=6) + timedelta(days=d)).isoformat()
                await _add_metric(store, "ga4_conversions", ts,
                                  10.0 if d < 6 else 25.0)
            action = await store.create_action(Action(
                workspace_id="test-ws", insight_id=ins.id, action_type="send_report",
                state="verifying", baseline_json={"metric": "ga4_conversions",
                                                  "demo": False}))
            await store._execute(
                "UPDATE actions SET dispatched_at = ? WHERE id = ?",
                (t0.isoformat(), action.id))
            await store._db.commit()
            return await attr.action_lift("test-ws", action.id, post_days=30,
                                          pre_days=30)

        res = asyncio.get_event_loop().run_until_complete(_run())
        assert res["metric"] == "ga4_conversions"
        assert res["lift_abs"] > 0 and res["lift_pct"] > 0
        assert res["significant"] is True
        assert "非随机实验" in res["caveat"] and "自助法" in res["method"]

    def test_action_lift_lookup_by_id_endpoint(self, env):
        import asyncio
        from fastapi.testclient import TestClient
        from insflow.core.entities import Action
        from insflow.server.app import app

        async def _run():
            store = env["store"]
            from insflow.core.entities import Insight
            ins = await store.create_insight(Insight(
                workspace_id="test-ws", type="t", title="t", summary="s"))
            await _add_metric(store, "ga4_conversions",
                              (datetime.now(UTC) - timedelta(days=1)).isoformat(), 5.0)
            return await store.create_action(Action(
                workspace_id="test-ws", insight_id=ins.id, action_type="x",
                baseline_json={"metric": "ga4_conversions"}))

        action = asyncio.get_event_loop().run_until_complete(_run())
        cli = TestClient(app)
        assert cli.get(f"/api/v1/attribution/lift/{action.id}",
                       params={"workspace_id": "test-ws"}).status_code == 200
        assert cli.get("/api/v1/attribution/lift/nope",
                       params={"workspace_id": "test-ws"}).status_code == 404


class TestNarrative:
    def test_parse_question(self):
        known = ["ga4_sessions", "ga4_conversions", "conversion_rate"]
        p = nar.parse_question("最近 30 天 ga4_sessions 的趋势如何", known)
        assert p["metric"] == "ga4_sessions" and p["days"] == 30 and p["intent"] == "trend"
        assert nar.parse_question("上周会话渠道分布", known)["days"] == 14
        assert nar.parse_question("数据质量怎么样", known)["intent"] == "data_quality"
        assert nar.parse_question("哪个渠道贡献最大", known)["intent"] == "attribution"
        assert nar.parse_question("动作增量如何", known)["intent"] == "lift"
        assert nar.parse_question("ga4_sessions 按地域", known)["dim"] == "province"
        # 长指标名优先（避免 conversion_rate 命中 ga4_conversions 子串）
        assert nar.parse_question("conversion_rate 多少", known)["metric"] == "conversion_rate"

    def test_narrate_sections(self, env):
        import asyncio
        from insflow.engine.demo import DemoSeeder

        async def _run():
            await DemoSeeder("test-ws", days=20).seed()
            return await nar.narrate("test-ws", "ga4_sessions", days=20)

        res = asyncio.get_event_loop().run_until_complete(_run())
        md = res["markdown"]
        for section in ("结论", "趋势外推", "数据质量", "建议", "口径"):
            assert section in md, section
        assert res["facts"]["total"] > 0
        assert res["citations"]

    def test_narrate_derived_metric_shows_lineage(self, env):
        import asyncio
        from insflow.engine.demo import DemoSeeder

        async def _run():
            store = env["store"]
            await DemoSeeder("test-ws", days=20).seed()
            await store.upsert_metric_def("test-ws", "cvr", label="转化率",
                                         expr="ga4_conversions / ga4_sessions * 100",
                                         unit="%", owner="growth")
            return await nar.narrate("test-ws", "cvr", days=20)

        res = asyncio.get_event_loop().run_until_complete(_run())
        assert "derived" in res["markdown"] or "口径：derived" in res["markdown"]
        assert "gis" not in res["markdown"]           # 无脚本注入

    def test_ask_metrics_intents(self, env):
        import asyncio
        from insflow.engine.demo import DemoSeeder

        async def _run():
            await DemoSeeder("test-ws", days=20).seed()
            out = {}
            for q in ("近 20 天 ga4_sessions 多少", "近 20 天 ga4_sessions 趋势",
                      "数据质量怎么样", "渠道归因", "动作增量如何", "完全无关的问题"):
                out[q] = await nar.ask_metrics("test-ws", q)
            return out

        out = asyncio.get_event_loop().run_until_complete(_run())
        assert out["近 20 天 ga4_sessions 多少"]["intent"] == "value"
        assert "ga4_sessions" in out["近 20 天 ga4_sessions 多少"]["answer"]
        assert out["近 20 天 ga4_sessions 趋势"]["intent"] == "trend"
        assert out["数据质量怎么样"]["intent"] == "data_quality"
        assert out["渠道归因"]["intent"] == "attribution"
        assert out["动作增量如何"]["intent"] == "lift"
        assert "没听懂" in out["完全无关的问题"]["answer"]

    def test_auto_insight_draft(self, env):
        import asyncio
        from insflow.engine.demo import DemoSeeder

        async def _run():
            await DemoSeeder("test-ws", days=20).seed()
            return await nar.auto_insight("test-ws", "ga4_sessions", days=20)

        draft = asyncio.get_event_loop().run_until_complete(_run())
        assert draft["type"] == "narrative_metric" and draft["title"]
        assert draft["severity"] in ("low", "medium", "high")
        assert draft["actions_json"] and draft["markdown"]


class TestWiring:
    def test_apis(self, env):
        import asyncio
        from fastapi.testclient import TestClient
        from insflow.engine.demo import DemoSeeder
        from insflow.server.app import app

        asyncio.get_event_loop().run_until_complete(
            DemoSeeder("test-ws", days=20).seed())
        cli = TestClient(app)
        assert cli.get("/api/v1/data-quality",
                       params={"workspace_id": "test-ws"}).status_code == 200
        assert cli.post("/api/v1/data-quality/backfill",
                        json={"workspace_id": "test-ws", "dry_run": True}).json()[
            "dry_run"] is True
        assert cli.get("/api/v1/attribution/channels",
                       params={"workspace_id": "test-ws"}).status_code == 200
        assert cli.get("/api/v1/attribution/channels",
                       params={"workspace_id": "test-ws",
                               "method": "bogus"}).status_code == 400
        assert cli.get("/api/v1/attribution/lift",
                       params={"workspace_id": "test-ws"}).status_code == 200
        narr = cli.post("/api/v1/narrative", json={"workspace_id": "test-ws",
                                                  "metric": "ga4_sessions"})
        assert narr.status_code == 200 and narr.json()["markdown"]
        pub = cli.post("/api/v1/narrative/insight",
                       json={"workspace_id": "test-ws", "metric": "ga4_sessions"})
        assert pub.status_code == 200 and pub.json()["insight_id"]

    def test_console_panels(self, env):
        import asyncio
        from fastapi.testclient import TestClient
        from insflow.engine.demo import DemoSeeder
        from insflow.server.app import app

        asyncio.get_event_loop().run_until_complete(
            DemoSeeder("test-ws", days=20).seed())
        cli = TestClient(app)
        ops = cli.get("/console/cockpit/ops", params={"workspace_id": "test-ws",
                                                     "days": 20})
        assert ops.status_code == 200 and "数据质量 SLA" in ops.text
        assert "dqsBackfill" in ops.text
        loop = cli.get("/console/cockpit/action-loop",
                       params={"workspace_id": "test-ws", "days": 20})
        assert loop.status_code == 200 and "渠道归因" in loop.text
        assert "动作增量" in loop.text
        ex = cli.get("/console/explore", params={"workspace_id": "test-ws",
                                                 "metric": "ga4_sessions"})
        assert ex.status_code == 200 and "exNarrate" in ex.text

    def test_cli(self, env):
        import asyncio
        from click.testing import CliRunner
        from insflow.cli import main
        from insflow.engine.demo import DemoSeeder

        asyncio.get_event_loop().run_until_complete(
            DemoSeeder("test-ws", days=20).seed())
        runner = CliRunner()
        for args in (["dq", "check", "-w", "test-ws"],
                     ["dq", "backfill", "-w", "test-ws", "--dry-run"],
                     ["attribution", "-w", "test-ws", "--method", "linear"],
                     ["lift", "-w", "test-ws"],
                     ["narrate", "-w", "test-ws", "-m", "ga4_sessions"]):
            res = runner.invoke(main, args)
            assert res.exit_code == 0, f"{args} -> {res.output[-300:]}"

    def test_agent_tools_registered(self):
        from insflow.agent.tools import AGENT_TOOLS, TOOL_EXECUTORS
        names = {t["function"]["name"] for t in AGENT_TOOLS}
        for tool in ("metric_qa", "narrate", "data_quality", "attribution",
                     "action_lift"):
            assert tool in names and tool in TOOL_EXECUTORS

    def test_scheduler_registers_dq(self):
        src = open("insflow/core/bootstrap.py", encoding="utf-8").read()
        assert "dq.daily" in src and "dq.daily@daily-09:45" in src
        assert "dq_auto_backfill" in src          # 自动续采需显式开启


class TestAgentInjectionRegression:
    """回归：新增工具必须按 schema 注入 workspace_id

    曾用硬编码白名单（只含 3 个工具），metric_qa 等新工具漏加 → 调用缺参静默失败，
    问数退化成洞察检索（表现为"答非所问"）。
    """

    def test_workspace_id_injected_from_schema(self, env):
        import asyncio
        from insflow.agent.agent import InsightAgent
        from insflow.engine.demo import DemoSeeder

        async def _run():
            await DemoSeeder("test-ws", days=20).seed()
            agent = InsightAgent("test-ws")
            for q in ("数据质量怎么样", "渠道归因", "动作增量如何",
                      "近 20 天 ga4_sessions 趋势"):
                out = await agent.ask(q)
                assert out.get("answer"), q
            return True

        assert asyncio.get_event_loop().run_until_complete(_run())

    def test_all_schema_tools_callable_with_only_domain_args(self, env):
        import asyncio
        import inspect
        from insflow.agent.agent import InsightAgent
        from insflow.agent.tools import AGENT_TOOLS, TOOL_EXECUTORS
        from insflow.engine.demo import DemoSeeder

        async def _run():
            await DemoSeeder("test-ws", days=20).seed()
            agent = InsightAgent("test-ws")
            results = {}
            for spec in AGENT_TOOLS:
                name = spec["function"]["name"]
                required = spec["function"]["parameters"].get("required", [])
                args = {}
                if "insight_id" in required:
                    continue                     # 需要真实 id，单独覆盖
                if "metric" in required:
                    args["metric"] = "ga4_sessions"
                if "question" in required:
                    args["question"] = "最近 20 天 ga4_sessions 多少"
                try:
                    payload = await agent.run_tool(name, args)
                    results[name] = json.loads(payload)
                except Exception as e:           # 工具内部异常也要返回 JSON error
                    results[name] = {"exception": str(e)}
            return results

        results = asyncio.get_event_loop().run_until_complete(_run())
        # 关键：不应再出现"缺 workspace_id"这类参数错误
        for name, payload in results.items():
            err = str(payload.get("error", "")) + str(payload.get("exception", ""))
            assert "workspace_id" not in err, f"{name}: {err}"
