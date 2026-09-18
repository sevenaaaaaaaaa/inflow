"""测试性能守则（TTL 缓存 / 指标聚合降采样 / 事件流轮转）"""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

import insflow.core.files as files_mod
from insflow.core.cache import TTLCache
from insflow.core.entities import Workspace
from insflow.core.files import EventBus
from insflow.core.store import Store, reset_store


class TestTTLCache:
    async def test_hit_and_expire(self):
        c = TTLCache(default_ttl=0.05)
        c.set("k", 1)
        assert c.get("k") == 1
        await asyncio.sleep(0.08)
        assert c.get("k") is None

    async def test_single_flight(self):
        """同一 key 并发只计算一次（防缓存击穿）"""
        c = TTLCache()
        calls = {"n": 0}

        async def factory():
            calls["n"] += 1
            await asyncio.sleep(0.02)
            return "value"

        results = await asyncio.gather(*[
            c.get_or_compute("same", factory) for _ in range(10)])
        assert results == ["value"] * 10
        assert calls["n"] == 1

    async def test_invalidate_prefix(self):
        c = TTLCache()
        c.set("cockpit:c1:x", 1)
        c.set("cockpit:c2:y", 2)
        assert c.invalidate("cockpit:c1") == 1
        assert c.get("cockpit:c1:x") is None
        assert c.get("cockpit:c2:y") == 2

    async def test_eviction_bounded(self):
        c = TTLCache(max_entries=10)
        for i in range(30):
            c.set(f"k{i}", i)
        assert len(c._data) <= 10

    async def test_stats(self):
        c = TTLCache()
        c.set("a", 1)
        c.get("a")
        c.get("miss")
        s = c.stats()
        assert s["hits"] == 1 and s["misses"] == 1 and s["hit_rate"] == 0.5


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)
    monkeypatch.setenv("INSFLOW_MASTER_KEY", "mk")
    s = Store(db_path=tmp_path / "test.db")
    await s.connect()
    await s.migrate()
    reset_store(s)
    await s.create_workspace(Workspace(id="test-ws", name="T"))
    yield {"store": s}
    reset_store(None)
    await s.close()


async def _seed_metric(store, metric, value, days_ago=0, entity="main"):
    ts = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    await store._execute(
        """INSERT INTO metrics (id, workspace_id, entity_type, entity_id, metric, value, dim_json, ts)
           VALUES (?, 'test-ws', 'topic', ?, ?, ?, '{}', ?)""",
        (f"m{metric}{days_ago}{value}{entity}", entity, metric, value, ts))
    await store._db.commit()


class TestMetricAggregation:
    async def test_series_buckets_by_day(self, env):
        store = env["store"]
        for d in (0, 1, 2):
            await _seed_metric(store, "topic_mentions_news", 5, days_ago=d)
        series = await store.metric_series("test-ws", "topic_mentions_news", days=7)
        assert len(series) == 3
        assert sum(p["value"] for p in series) == 15

    async def test_series_hour_bucket_for_short_range(self, env):
        store = env["store"]
        await _seed_metric(store, "x", 1)
        series = await store.metric_series("test-ws", "x", days=1)
        assert series and len(series[0]["bucket"]) == 13  # YYYY-MM-DD HH

    async def test_series_respects_entity_filter(self, env):
        store = env["store"]
        await _seed_metric(store, "m", 1, entity="a")
        await _seed_metric(store, "m", 2, entity="b")
        series = await store.metric_series("test-ws", "m", days=7, entity_id="a")
        assert sum(p["value"] for p in series) == 1

    async def test_totals_single_query(self, env):
        store = env["store"]
        await _seed_metric(store, "a", 3)
        await _seed_metric(store, "b", 4)
        totals = await store.metric_totals("test-ws", ["a", "b", "missing"])
        assert totals["a"]["value"] == 3
        assert totals["b"]["value"] == 4
        assert "missing" not in totals

    async def test_avg_agg_for_ratios(self, env):
        store = env["store"]
        await _seed_metric(store, "ratio", 0.2)
        await _seed_metric(store, "ratio", 0.4)
        totals = await store.metric_totals("test-ws", ["ratio"], agg="avg")
        assert 0.25 < totals["ratio"]["value"] < 0.35

    async def test_breakdown_by_entity(self, env):
        store = env["store"]
        await _seed_metric(store, "r", 0.1, entity="A")
        await _seed_metric(store, "r", 0.6, entity="B")
        rows = await store.metric_breakdown("test-ws", "r", days=7)
        assert rows[0]["entity_id"] == "B"  # 降序

    async def test_series_limit_cap(self, env):
        store = env["store"]
        for d in range(30):
            await _seed_metric(store, "d", 1, days_ago=d)
        series = await store.metric_series("test-ws", "d", days=90, limit=10)
        assert len(series) <= 10

    async def test_index_created(self, env):
        """V6 迁移：分析索引存在"""
        store = env["store"]
        rows = await store._fetchall(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_metrics_ws%'")
        names = {r["name"] for r in rows}
        assert "idx_metrics_ws_metric_ts" in names
        assert "idx_metrics_ws_entity_metric_ts" in names


class TestEventRotation:
    def test_rotate_keeps_recent(self, tmp_path):
        bus = EventBus("ws", base_dir=tmp_path)
        bus.emit("new.event", {"x": 1})
        # 手动写入一条 60 天前的事件
        old = {"ts": (datetime.now(UTC) - timedelta(days=60)).isoformat(),
               "type": "old.event", "workspace": "ws", "payload": {}}
        with open(bus.events_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(old) + "\n")

        result = bus.rotate(keep_days=30)
        assert result["dropped"] == 1
        remaining = bus.read()
        assert all(e["type"] != "old.event" for e in remaining)

    def test_rotate_noop_when_empty(self, tmp_path):
        bus = EventBus("ws", base_dir=tmp_path)
        assert bus.rotate(keep_days=30) == {"kept": 0, "dropped": 0}


class TestFrequencyGovernor:
    """频率治理（对齐 OpenFlow 心跳降频）"""

    def test_parse_cron_minutes(self):
        from insflow.core.governor import parse_cron_minutes
        assert parse_cron_minutes("*/5 * * * *") == 5.0
        assert parse_cron_minutes("0 */6 * * *") == 360.0
        assert parse_cron_minutes("0 2 * * *") == 1440.0
        assert parse_cron_minutes("30 9 * * 1") == 1440.0
        assert parse_cron_minutes("bad") is None

    def test_validate_cron_blocks_too_frequent(self):
        from insflow.core.governor import CronTooFrequent, validate_cron
        with pytest.raises(CronTooFrequent):
            validate_cron("keyword", "*/30 * * * *")     # keyword 最小 360 分钟
        with pytest.raises(CronTooFrequent):
            validate_cron("site_change", "*/5 * * * *")  # 最小 30 分钟

    def test_validate_cron_allows_reasonable(self):
        from insflow.core.governor import validate_cron
        validate_cron("keyword", "0 */6 * * *")
        validate_cron("site_change", "0 */2 * * *")
        validate_cron("topic", "0 * * * *")

    async def test_monitor_create_rejects_high_frequency(self, tmp_path, monkeypatch):
        monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)
        from insflow.core.entities import Workspace
        from insflow.core.governor import CronTooFrequent as CTF
        from insflow.core.scheduler import Scheduler
        from insflow.engine.monitors import MonitorService
        s = Store(db_path=tmp_path / "t.db")
        await s.connect(); await s.migrate(); reset_store(s)
        await s.create_workspace(Workspace(id="test-ws", name="T"))
        svc = MonitorService("test-ws", scheduler=Scheduler("test-ws"))
        with pytest.raises(CTF):
            await svc.create("keyword", {"site": "x"}, "*/10 * * * *")
        await s.close()

    def test_event_dedupe_merges_repeats(self, tmp_path):
        from insflow.core.governor import emit_throttled
        bus = EventBus("ws", base_dir=tmp_path)
        assert emit_throttled(bus, "monitor.run_finished", {"monitor_id": "m1"}) is True
        assert emit_throttled(bus, "monitor.run_finished", {"monitor_id": "m1"}) is False
        assert emit_throttled(bus, "monitor.run_finished", {"monitor_id": "m2"}) is True
        # 关键事件不降频
        assert emit_throttled(bus, "quota.exceeded", {"kind": "api_calls"}) is True
        assert emit_throttled(bus, "quota.exceeded", {"kind": "api_calls"}) is True
        assert len(bus.read(event_type="monitor.run_finished")) == 2


class TestStoreHealth:
    async def test_health_metrics(self, env):
        store = env["store"]
        h = await store.health()
        assert h["db_size_bytes"] >= 0
        assert "insights" in h["row_counts"]
        assert h["queries"] >= 0
        assert "p95_ms" in h and "slow_queries" in h

    async def test_slow_query_counter(self, env):
        store = env["store"]
        before = (await store.health())["queries"]
        await store._fetchone("SELECT COUNT(*) AS n FROM metrics")
        assert (await store.health())["queries"] > before

    async def test_wal_checkpoint(self, env):
        await env["store"].wal_checkpoint()   # 不抛异常即可

    async def test_db_path_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("INSFLOW_DB_PATH", str(tmp_path / "custom.db"))
        from insflow.core.store import default_db_path
        assert default_db_path() == tmp_path / "custom.db"


class TestFileCacheBackend:
    def test_file_backend_roundtrip(self, tmp_path):
        from insflow.core.cache import FileBackend
        b = FileBackend(tmp_path)
        b.set("k", {"v": 1}, 60)
        assert b.get("k") == {"v": 1}
        b.clear()
        assert b.get("k") is None

    def test_file_backend_expiry(self, tmp_path):
        import time

        from insflow.core.cache import FileBackend
        b = FileBackend(tmp_path)
        b.set("k", "v", 0.01)
        time.sleep(0.03)
        assert b.get("k") is None
