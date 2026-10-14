"""测试调度器日历守卫（非工作日跳过 + 夜间窗口 06:00 强制截止）"""

import asyncio

import insflow.core.scheduler as sched_mod
from insflow.core.scheduler import Scheduler


def _workday(monkeypatch):
    monkeypatch.setattr(sched_mod, "is_workday", lambda *a, **k: True)


def _non_workday(monkeypatch):
    monkeypatch.setattr(sched_mod, "is_workday", lambda *a, **k: False)


class TestGuard:
    def test_important_always_passes(self, monkeypatch):
        _non_workday(monkeypatch)
        s = Scheduler("test-ws")
        assert s._guard("monitor.run", {"important": True}) is None
        assert s._guard("monitor.run", {"target_json": {"important": True}}) is None

    def test_non_important_blocked_on_holiday(self, monkeypatch):
        _non_workday(monkeypatch)
        s = Scheduler("test-ws")
        assert s._guard("monitor.run", {}) == "non-workday"
        assert s._guard("agent.task.run", {}) == "non-workday"

    def test_system_jobs_not_gated(self, monkeypatch):
        """系统级定时任务（备份/轮转/周任务）不受日历守卫误伤"""
        _non_workday(monkeypatch)
        s = Scheduler("test-ws")
        assert s._guard("backup.daily", {}) is None
        assert s._guard("snapshot.cleanup", {}) is None
        # 显式 workday_only 的普通任务仍受管
        assert s._guard("custom.job", {"workday_only": True}) == "non-workday"

    def test_workday_passes(self, monkeypatch):
        _workday(monkeypatch)
        s = Scheduler("test-ws")
        assert s._guard("monitor.run", {}) is None


class TestRunTaskGuard:
    async def test_skipped_on_non_workday(self, monkeypatch):
        _non_workday(monkeypatch)
        s = Scheduler("test-ws")
        ran = {"n": 0}

        async def handler(payload=None):
            ran["n"] += 1

        s.register_handler("agent.task.run", handler)
        await s._run_task("agent.task.run", {"task_id": "t1"})
        assert ran["n"] == 0  # 守卫拦截，handler 未执行

    async def test_runs_on_workday(self, monkeypatch):
        _workday(monkeypatch)
        s = Scheduler("test-ws")
        ran = {"n": 0}

        async def handler(payload=None):
            ran["n"] += 1

        s.register_handler("agent.task.run", handler)
        await s._run_task("agent.task.run", {"task_id": "t1"})
        assert ran["n"] == 1

    async def test_non_important_killed_at_night_end(self, monkeypatch):
        """非重要任务超时被强制结束（夜间窗口截止语义）"""
        _workday(monkeypatch)
        # 模拟「距 06:00 只剩 0.05s」：把截止秒数 patch 到极小
        monkeypatch.setattr(sched_mod, "seconds_until_night_end", lambda: 0.05)
        s = Scheduler("test-ws")

        async def slow_handler(payload=None):
            await asyncio.sleep(5)  # 远超 0.05s 的窗口

        s.register_handler("monitor.run", slow_handler)
        await asyncio.wait_for(
            s._run_monitor("m1", {"kind": "topic", "target_json": {}}), timeout=3)
        # 未抛超时异常即通过：_run_monitor 内部捕获 TimeoutError → monitor.alert

    async def test_important_not_time_limited(self, monkeypatch):
        _workday(monkeypatch)
        s = Scheduler("test-ws")
        ran = {"n": 0}

        async def handler(payload=None):
            await asyncio.sleep(0.1)
            ran["n"] += 1

        s.register_handler("monitor.run", handler)
        await s._run_monitor("m1", {"kind": "topic", "target_json": {"important": True}})
        assert ran["n"] == 1
