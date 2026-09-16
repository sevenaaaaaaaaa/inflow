"""测试监控任务服务（CRUD + 调度持久化 + 执行编排）"""

import pytest

import insflow.core.files as files_mod
from insflow.core.entities import Workspace
from insflow.core.scheduler import Scheduler
from insflow.core.store import Store, get_store, reset_store
from insflow.engine.monitors import MonitorService


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)

    s = Store(db_path=tmp_path / "test.db")
    await s.connect()
    await s.migrate()
    reset_store(s)
    await s.create_workspace(Workspace(id="test-ws", name="Test WS"))

    sched = Scheduler("test-ws")
    svc = MonitorService("test-ws", scheduler=sched)
    yield {"store": s, "service": svc, "scheduler": sched}

    sched.shutdown()
    reset_store(None)
    await s.close()


class TestCRUD:
    async def test_create_and_list(self, env):
        svc = env["service"]
        m = await svc.create("site_change", {"url": "https://c.com/pricing"})
        assert m["state"] == "idle"
        assert m["schedule_cron"] == "0 */6 * * *"
        assert len(await svc.list()) == 1

    async def test_unknown_kind_rejected(self, env):
        with pytest.raises(ValueError):
            await env["service"].create("bad-kind", {})

    async def test_delete_removes_job(self, env):
        svc = env["service"]
        m = await svc.create("site_change", {"url": "https://c.com"})
        assert await svc.delete(m["id"]) is True
        assert await svc.list() == []

    async def test_get_not_found(self, env):
        result = await env["service"].run("ghost")
        assert "error" in result


class TestScheduling:
    async def test_job_registered(self, env):
        svc = env["service"]
        env["scheduler"].start()
        m = await svc.create("site_change", {"url": "https://c.com/pricing"},
                             schedule_cron="0 */2 * * *")
        jobs = env["scheduler"].list_jobs()
        assert f"monitor.{m['id']}" in {j["id"] for j in jobs}

    async def test_restore_all(self, env):
        """调度持久化：DB 中的任务可恢复到调度器"""
        svc = env["service"]
        m1 = await svc.create("site_change", {"url": "https://a.com"}, "0 1 * * *")
        m2 = await svc.create("keyword", {"query": "增长工具"}, "0 2 * * *")

        # 模拟重启：新 service + 空 scheduler
        sched2 = Scheduler("test-ws")
        svc2 = MonitorService("test-ws", scheduler=sched2)
        restored = await svc2.restore_all()
        assert restored == 2
        sched2.shutdown()

    async def test_restore_empty(self, env):
        sched2 = Scheduler("test-ws")
        svc2 = MonitorService("test-ws", scheduler=sched2)
        assert await svc2.restore_all() == 0
        sched2.shutdown()


class TestExecution:
    async def test_run_site_change_first_run(self, env, monkeypatch):
        """site_change：Firecrawl 注入 fake → 首次运行只存快照"""
        svc = env["service"]
        m = await svc.create("site_change", {"url": "https://c.com/pricing"})

        async def fake_fetch(self, target):
            return "# Pricing $49"

        monkeypatch.setattr(MonitorService, "_fetch_page", fake_fetch)
        result = await svc.run(m["id"])
        assert result["first_run"] is True

        from insflow.core.store import get_store
        monitor = await (await get_store()).get_monitor("test-ws", m["id"])
        assert monitor["state"] == "ok"

    async def test_run_error_state(self, env, monkeypatch):
        svc = env["service"]
        m = await svc.create("site_change", {"url": "https://c.com/x"})

        async def fake_fetch(self, target):
            raise RuntimeError("network down")

        monkeypatch.setattr(MonitorService, "_fetch_page", fake_fetch)
        result = await svc.run(m["id"])
        assert "error" in result

        from insflow.core.store import get_store
        monitor = await (await get_store()).get_monitor("test-ws", m["id"])
        assert monitor["state"] == "error"
