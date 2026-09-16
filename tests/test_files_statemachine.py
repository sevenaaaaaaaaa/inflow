"""测试状态机 + 事件流 + 报告存储"""


import pytest

from insflow.core.files import EventBus, ReportStore, SnapshotStore
from insflow.core.statemachine import (
    ACTION_MACHINE,
    INSIGHT_MACHINE,
    MONITOR_MACHINE,
    InvalidTransition,
)


class TestStateMachine:
    def test_valid_transition(self):
        assert ACTION_MACHINE.validate("pending", "dispatched") is True

    def test_invalid_transition(self):
        with pytest.raises(InvalidTransition):
            ACTION_MACHINE.validate("pending", "done")

    def test_action_flow(self):
        # pending → dispatched → done → verifying → verified
        flow = ["pending", "dispatched", "done", "verifying", "verified"]
        for cur, nxt in zip(flow, flow[1:]):
            assert ACTION_MACHINE.can(cur, nxt)

    def test_failed_retry_flow(self):
        # failed 可以重试回 pending
        assert ACTION_MACHINE.can("failed", "pending")
        # pending → dispatched → failed → dead（重试耗尽）
        assert ACTION_MACHINE.can("failed", "dead")

    def test_monitor_flow(self):
        assert MONITOR_MACHINE.can("idle", "running")
        assert MONITOR_MACHINE.can("running", "ok")
        assert MONITOR_MACHINE.can("running", "error")
        assert not MONITOR_MACHINE.can("idle", "ok")

    def test_insight_flow(self):
        assert INSIGHT_MACHINE.can("new", "acknowledged")
        assert INSIGHT_MACHINE.can("acknowledged", "actioned")
        assert INSIGHT_MACHINE.can("actioned", "verified")
        assert INSIGHT_MACHINE.can("new", "dismissed")
        assert not INSIGHT_MACHINE.can("new", "verified")


class TestEventBus:
    def test_emit_and_read(self, tmp_path):
        bus = EventBus("test-ws", base_dir=tmp_path)
        event = bus.emit("insight.created", {"id": "abc123"})

        assert event["type"] == "insight.created"
        events = bus.read()
        assert len(events) == 1
        assert events[0]["payload"]["id"] == "abc123"

    def test_filter_by_type(self, tmp_path):
        bus = EventBus("test-ws", base_dir=tmp_path)
        bus.emit("insight.created", {"id": "1"})
        bus.emit("monitor.alert", {"id": "2"})
        bus.emit("insight.created", {"id": "3"})

        insights = bus.read(event_type="insight.created")
        assert len(insights) == 2
        alerts = bus.read(event_type="monitor.alert")
        assert len(alerts) == 1

    def test_limit(self, tmp_path):
        bus = EventBus("test-ws", base_dir=tmp_path)
        for i in range(10):
            bus.emit("test.event", {"i": i})
        events = bus.read(limit=3)
        assert len(events) == 3
        # 应该是最新的 3 条
        assert events[0]["payload"]["i"] == 7


class TestReportStore:
    def test_save_weekly_report(self, tmp_path):
        store = ReportStore("test-ws", base_dir=tmp_path)
        path = store.save_report("diagnosis", "# 诊断报告\n内容")
        assert path.exists()
        assert "W" in path.name  # ISO 周命名

    def test_save_monthly_report(self, tmp_path):
        store = ReportStore("test-ws", base_dir=tmp_path)
        path = store.save_report("maturity", "# 成熟度报告")
        assert path.exists()
        # 月份命名 YYYY-MM
        assert len(path.stem.split("-")) == 2

    def test_list_and_get(self, tmp_path):
        store = ReportStore("test-ws", base_dir=tmp_path)
        store.save_report("diagnosis", "# 报告A")
        store.save_report("competitors", "# 竞品周报")

        # 不同类别各自一份
        assert len(store.list_reports("diagnosis")) == 1
        assert len(store.list_reports("competitors")) == 1

        content = store.get_report("diagnosis", store.list_reports("diagnosis")[0].name)
        assert "报告A" in content


class TestSnapshotStore:
    def test_save_and_get_latest(self, tmp_path):
        store = SnapshotStore("test-ws", base_dir=tmp_path)
        store.save_snapshot("mon-1", "<html>v1</html>")
        store.save_snapshot("mon-1", "<html>v2</html>")

        latest = store.get_latest_snapshot("mon-1")
        assert latest is not None
        # v2 应该覆盖同一天的快照
        assert "v2" in latest[1]

    def test_empty(self, tmp_path):
        store = SnapshotStore("test-ws", base_dir=tmp_path)
        assert store.get_latest_snapshot("nonexistent") is None
