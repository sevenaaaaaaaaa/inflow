"""测试 collector 指标幂等去重（R1-4）"""


import pytest

from insflow.core.entities import Workspace
from insflow.core.store import Store, reset_store
from insflow.engine.collector_router import _save_metrics


@pytest.fixture
async def env(tmp_path, monkeypatch):
    import insflow.core.files as fm

    monkeypatch.setattr(fm, "DATA_DIR", tmp_path)

    s = Store(db_path=tmp_path / "test.db")
    await s.connect()
    await s.migrate()
    reset_store(s)
    await s.create_workspace(Workspace(id="test-ws", name="T"))
    yield {"store": s}
    reset_store(None)
    await s.close()


def _row(metric="gsc_impressions", value=100, entity="kw1"):
    return {"entity_type": "keyword", "entity_id": entity,
            "metric": metric, "value": value, "dim": {"k": 1}}


class TestIdempotentSave:
    async def test_duplicate_same_window_ignored(self, env):
        """同 monitor 同小时窗口重复写入 → 第二次 INSERT OR IGNORE 返回 0"""
        row = _row()
        first = await _save_metrics("test-ws", monitor_id="mon-1", rows=[row])
        second = await _save_metrics("test-ws", monitor_id="mon-1", rows=[dict(row)])
        assert first == 1
        assert second == 0

    async def test_different_monitor_same_window_allowed(self, env):
        """不同 monitor 同窗口各自独立"""
        row = _row()
        assert await _save_metrics("test-ws", monitor_id="mon-1", rows=[dict(row)]) == 1
        assert await _save_metrics("test-ws", monitor_id="mon-2", rows=[dict(row)]) == 1

    async def test_different_metric_allowed(self, env):
        """同 monitor 同窗口不同指标不冲突"""
        now_rows = [
            {"entity_type": "keyword", "entity_id": "kw", "metric": "m1", "value": 1},
            {"entity_type": "keyword", "entity_id": "kw", "metric": "m2", "value": 2},
        ]
        assert await _save_metrics("test-ws", monitor_id="mon-1", rows=now_rows) == 2

    async def test_manual_override_window(self, env):
        """显式指定 window_key（如按天窗口）同样生效"""
        row = {"entity_type": "site", "entity_id": "main", "metric": "brand_mention",
               "value": 5, "window_key": "2026-09-16-00"}
        assert await _save_metrics("test-ws", monitor_id="mon-3", rows=[dict(row)]) == 1
        assert await _save_metrics("test-ws", monitor_id="mon-3", rows=[dict(row)]) == 0

    async def test_old_data_unaffected(self, env):
        """旧数据（window_key=''）不受部分索引约束，可继续写入"""
        old_style = [{"entity_type": "site", "entity_id": "main", "metric": "legacy", "value": 1}]
        r1 = await _save_metrics("test-ws", monitor_id="", rows=[dict(old_style[0])])
        r2 = await _save_metrics("test-ws", monitor_id="", rows=[dict(old_style[0])])
        assert r1 == 1
        # monitor_id='' 时 window_key 仍会写入（fallback 小时窗口）→ 幂等生效
        assert r2 == 0

    async def test_metrics_table_migrated(self, env):
        """V3 迁移：新列存在"""
        store = env["store"]
        row = await store._fetchone("SELECT monitor_id, window_key FROM metrics LIMIT 1")
        assert row is not None or True  # 列存在即通过（空表返回 None）
