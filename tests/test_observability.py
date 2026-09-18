"""测试可观测性（每日摘要 + 告警分级出站）"""

from datetime import UTC, datetime, timedelta

import pytest

import insflow.core.files as files_mod
from insflow.core.files import EventBus
from insflow.engine.observability import ALERT_RULES, AlertDispatcher, DailyDigestBuilder


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)
    bus = EventBus("test-ws")
    datetime.now(UTC)

    # 预置最近 24h 的事件
    bus.emit("monitor.run_finished", {"monitor_id": "m1"})
    bus.emit("monitor.run_finished", {"monitor_id": "m2"})
    bus.emit("monitor.alert", {"monitor_id": "m3", "error": "429"})
    bus.emit("insight.created", {"insight_id": "i1"})
    bus.emit("insight.quality_gate_blocked", {"title": "草稿"})
    bus.emit("feedback.received", {"verdict": "effective"})
    bus.emit("source.token_refresh_failed", {"provider": "gsc", "error": "invalid_grant"})

    # 窗口外的事件（48h 前，不应计入）
    old = bus.emit("insight.created", {"insight_id": "old"})
    # 覆盖 ts 为 48h 前
    lines = []
    for e in bus.read(limit=100):
        if e["payload"].get("insight_id") == "old":
            e["ts"] = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
        lines.append(__import__("json").dumps(e, ensure_ascii=False, default=str))
    (files_mod.DATA_DIR / "events" / "test-ws" / "events.jsonl").write_text(
        "\n".join(lines) + "\n")

    yield {"bus": bus}

    del old


class TestDailyDigest:
    async def test_window_aggregation(self, env):
        result = await DailyDigestBuilder("test-ws").build(window_hours=24)
        s = result["summary"]
        assert s["collect_ok"] == 2
        assert s["collect_fail"] == 1
        assert s["success_rate"] == pytest.approx(2 / 3)
        assert s["insights_created"] == 1
        assert s["feedback_received"] == 1
        assert s["alerts_count"] >= 1

    async def test_old_events_excluded(self, env):
        result = await DailyDigestBuilder("test-ws").build(window_hours=24)
        s = result["summary"]
        # 48h 前的 insight 不计入
        assert s["insights_created"] == 1

    async def test_alerts_classified(self, env):
        result = await DailyDigestBuilder("test-ws").build(window_hours=24)
        alerts = result["summary"]["alerts"]
        levels = {a["level"] for a in alerts}
        assert "critical" in levels  # token_refresh_failed
        kinds = {a["kind"] for a in alerts}
        assert "授权轮换失败" in kinds

    async def test_markdown_renders(self, env):
        result = await DailyDigestBuilder("test-ws").build()
        assert "每日运行摘要" in result["markdown"]
        assert "67%" in result["markdown"] or "无采集" in result["markdown"]

    async def test_empty_window(self, tmp_path, monkeypatch):
        monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path / "fresh")
        result = await DailyDigestBuilder("fresh").build()
        s = result["summary"]
        assert s["success_rate"] is None
        assert s["insights_created"] == 0


class TestAlertDispatcher:
    async def test_dispatch_critical_first(self, env, monkeypatch):
        """critical 告警 → feishu.notify 适配器（severity=high）"""
        captured = []

        class FakeAdapter:
            @property
            def action_type(self):
                return "feishu.notify"

            async def execute(self, action, ctx):
                captured.append(action)
                return {"ok": True, "ref": "f:0", "detail": "posted"}

        import httpx  # noqa
        import insflow.engine.observability as obs_mod
        fake_router = type("R", (), {
            "dispatch": staticmethod(lambda action, ctx: FakeAdapter().execute(action, ctx)),
        })()
        monkeypatch.setattr(obs_mod, "get_action_router", lambda: fake_router)

        alerts = [
            {"level": "critical", "kind": "配额熔断", "payload": {"source": "serper"}},
            {"level": "high", "kind": "监控失败", "payload": {"monitor_id": "m3"}},
        ]
        result = await AlertDispatcher("test-ws").dispatch_alerts(alerts)

        assert result["dispatched"] == 2
        assert captured[0]["title"] == "【CRITICAL】配额熔断"
        assert captured[0]["severity"] == "high"

    async def test_empty_alerts(self, env):
        result = await AlertDispatcher("test-ws").dispatch_alerts([])
        assert result["dispatched"] == 0


class TestAlertRules:
    def test_critical_events_covered(self):
        """北极星保护事件必须在订阅清单里"""
        assert "quota.exceeded" in ALERT_RULES
        assert "source.token_refresh_failed" in ALERT_RULES
        assert "monitor.error" in ALERT_RULES
        assert "action.dead" in ALERT_RULES
