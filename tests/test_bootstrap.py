"""测试启动引导（定时任务集成）"""

import pytest

import insflow.core.files as files_mod
from insflow.core.bootstrap import bootstrap_scheduled_jobs
from insflow.core.entities import Workspace
from insflow.core.store import Store, reset_store


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)

    s = Store(db_path=tmp_path / "test.db")
    await s.connect()
    await s.migrate()
    reset_store(s)
    await s.create_workspace(Workspace(id="test-ws", name="Test WS"))

    yield {"store": s}

    # 清理调度器
    from insflow.core.scheduler import get_scheduler
    get_scheduler().shutdown()
    reset_store(None)
    await s.close()


class TestBootstrap:
    async def test_bootstrap_registers_jobs(self, env, monkeypatch):
        import insflow.core.scheduler as sched_mod
        monkeypatch.setattr(sched_mod, "_scheduler", None)

        registered = await bootstrap_scheduled_jobs()

        assert registered["monitors_restored"] == 0  # 无监控任务
        assert "feedback.evaluate@daily-09:00" in registered["jobs"]
        assert "report.weekly@mon-09:30" in registered["jobs"]
        assert "quota.audit@hourly" in registered["jobs"]

        from insflow.core.scheduler import get_scheduler
        jobs = get_scheduler().list_jobs()
        ids = {j["id"] for j in jobs}
        assert {"feedback.evaluate-daily", "report.weekly", "quota.audit"} <= ids

    async def test_restores_monitors(self, env):
        """已有监控任务 → 启动时自动恢复到调度器"""
        import insflow.core.scheduler as sched_mod
        monkeypatch = env
        store = env["store"]

        await store.create_monitor("test-ws", "site_change",
                                   {"url": "https://c.com/pricing"})

        from insflow.core.scheduler import get_scheduler
        get_scheduler().shutdown()
        monkeypatch_setattr = None
        # 重置调度器单例后重新引导
        sched_mod._scheduler = None

        registered = await bootstrap_scheduled_jobs()
        assert registered["monitors_restored"] == 1
