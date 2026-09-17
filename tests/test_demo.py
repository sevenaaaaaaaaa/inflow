"""测试演示数据生成器（驾驶舱可视化验收）"""

import pytest

import insflow.core.files as files_mod
from insflow.core.entities import Workspace
from insflow.core.store import Store, get_store, reset_store
from insflow.engine.demo import DemoSeeder
from insflow.web.cockpit import COCKPITS


@pytest.fixture
async def seeded(tmp_path, monkeypatch):
    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)
    monkeypatch.setenv("INSFLOW_MASTER_KEY", "mk")
    s = Store(db_path=tmp_path / "test.db")
    await s.connect()
    await s.migrate()
    reset_store(s)
    await s.create_workspace(Workspace(id="demo", name="D"))
    await DemoSeeder("demo", days=10).seed()
    yield {"store": s}
    reset_store(None)
    await s.close()


class TestDemoData:
    async def test_all_cockpits_populated(self, seeded):
        """9 舱全部有数据（这是 demo 的核心目的）"""
        o = await COCKPITS["overview"]("demo")
        assert o["kpis"]["recent"] > 0 and o["risk"]
        assert await COCKPITS["sentiment"]("demo")
        s = await COCKPITS["sentiment"]("demo")
        assert s["kpis"]["mentions"] > 0 and s["words"] and s["alerts"]
        t = await COCKPITS["traffic"]("demo")
        assert t["kpis"]["clicks"] > 0 and t["scatter"] and 0 < t["kpis"]["ctr"] < 1
        c = await COCKPITS["competitor"]("demo")
        assert c["kpis"]["competitors"] == 3 and c["gaps"]
        j = await COCKPITS["journey"]("demo")
        assert len(j["funnel"]) >= 3
        a = await COCKPITS["action-loop"]("demo")
        assert a["kpis"]["total"] > 0 and a["effectiveness"]
        ops = await COCKPITS["ops"]("demo")
        assert ops["kpis"]["ok"] > 0 and ops["alerts"]
        b = await COCKPITS["billing"]("demo")
        assert any(g[1] > 0 for g in b["gauges"])
        r = await COCKPITS["reports"]("demo")
        assert r["total"] >= 1

    async def test_clear_removes_demo_only(self, seeded):
        """清理只删 demo 标记行（不动真实数据）"""
        store = seeded["store"]
        # 插一条真实洞察
        from insflow.core.entities import Insight
        real = await store.create_insight(Insight(
            workspace_id="demo", type="real", title="真实洞察", summary="x",
            evidence_json=[{"type": "real"}]))
        counts = await DemoSeeder("demo").clear()
        assert counts["insights"] > 0
        assert await store.get_insight(real.id) is not None
        demo_left = [i for i in await store.list_insights("demo")
                     if i.type == "topic_negative_alert"]
        assert not demo_left
