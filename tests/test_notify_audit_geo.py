"""Batch7 测试：通知策略 / 管理审计 / 性能基准 / GeoJSON 边界地图 / 每日摘要调度"""

import json

import pytest

import insflow.core.files as files_mod
from insflow.core.entities import Workspace
from insflow.core.store import Store, generate_id, reset_store
from insflow.engine import geo as geo_mod
from insflow.engine.notify_policy import (QuietHours, Throttle, defer, drain_pending,
                                         escalation_targets, group_key, peek_pending,
                                         pending_count, policy_of)
from insflow.viz import charts as c

TINY_GEOJSON = {
    "type": "FeatureCollection",
    "features": [
        {"type": "Feature", "properties": {"name": "甲区"},
         "geometry": {"type": "Polygon",
                      "coordinates": [[[100, 30], [110, 30], [110, 40], [100, 40],
                                       [100, 30]]]}},
        {"type": "Feature", "properties": {"name": "乙区"},
         "geometry": {"type": "MultiPolygon",
                      "coordinates": [[[[110, 30], [120, 30], [120, 40], [110, 40],
                                        [110, 30]]]]}},
        {"type": "Feature", "properties": {"other": "no-name"},
         "geometry": {"type": "Polygon",
                      "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}},
    ],
}


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


class TestIdCollision:
    def test_ids_unique_at_scale(self):
        ids = {generate_id() for _ in range(200_000)}
        assert len(ids) == 200_000          # 旧实现（8 位）在此规模会碰撞
        assert len(next(iter(ids))) == 16


class TestNotifyPolicy:
    def test_quiet_hours_cross_midnight(self):
        q = QuietHours({"start": "22:00", "end": "08:00", "tz_offset": 8})
        from datetime import UTC, datetime
        assert q.active(datetime(2026, 9, 17, 15, 30, tzinfo=UTC))     # 北京 23:30
        assert not q.active(datetime(2026, 9, 17, 2, 0, tzinfo=UTC))   # 北京 10:00
        assert "T" in q.next_resume(datetime(2026, 9, 17, 15, 30, tzinfo=UTC))

    def test_quiet_hours_weekend_and_disabled(self):
        from datetime import UTC, datetime
        sat = datetime(2026, 9, 19, 4, 0, tzinfo=UTC)
        assert QuietHours({"start": "22:00", "end": "08:00", "weekend": True}).active(sat)
        assert not QuietHours({}).active(sat)

    def test_throttle_merges(self):
        t = Throttle()
        first, _ = t.check("g", 900)
        second, similar = t.check("g", 900)
        other, _ = t.check("h", 900)
        assert first and not second and similar == 1 and other

    def test_escalation_chain_progressive(self):
        chain = [{"after_minutes": 0, "to": ["webhook"]},
                 {"after_minutes": 60, "to": ["feishu", "email"]}]
        assert escalation_targets(chain, 0) == ["webhook"]
        assert escalation_targets(chain, 61) == ["webhook", "feishu", "email"]
        assert escalation_targets(None, 10) == []

    def test_group_key_modes(self):
        alert = {"metric": "ga4_sessions", "rule_id": "r1"}
        assert group_key(alert, {}) == "ga4_sessions"
        assert group_key(alert, {"group_by": "rule"}) == "r1"
        assert group_key(alert, {"group_by": "none"}) == "-"

    def test_defer_and_drain_per_workspace(self):
        defer({"metric": "m", "workspace_id": "w1"}, "quiet")
        defer({"metric": "m", "workspace_id": "w2"}, "quiet")
        assert pending_count() >= 2
        assert len(peek_pending("w1")) == 1
        assert len(drain_pending("w1")) == 1
        assert all(i["alert"].get("workspace_id") != "w1" for i in drain_pending())

    def test_alerts_respects_quiet_hours(self, env):
        import asyncio

        async def _run():
            store = env["store"]
            ws = await store.get_workspace("test-ws")
            settings = dict(ws.settings_json or {})
            settings["notify_policy"] = {"quiet_hours": {"start": "00:00",
                                                         "end": "23:59",
                                                         "tz_offset": 0}}
            ws.settings_json = settings
            await store.update_workspace(ws)
            from insflow.engine.alerts import notify_with_policy
            return await notify_with_policy("test-ws", "t", "s", ["webhook"], {},
                                            {"metric": "ga4_sessions", "name": "r",
                                             "workspace_id": "test-ws"})

        out = asyncio.get_event_loop().run_until_complete(_run())
        assert out.get("deferred") == "quiet_hours" and out.get("pending", 0) >= 1
        assert policy_of({"notify_policy": {"grouping": {"min_interval_s": 60}}}) == {
            "grouping": {"min_interval_s": 60}}


class TestAdminAudit:
    async def _seed_demo(self, env, days: int = 10):
        from insflow.engine.demo import DemoSeeder
        await DemoSeeder("test-ws", days=days).seed()

    def test_record_and_list_and_csv(self, env):
        import asyncio
        from fastapi.testclient import TestClient
        from insflow.server.app import app

        asyncio.get_event_loop().run_until_complete(self._seed_demo(env))
        cli = TestClient(app)
        # 各类写操作都应留痕
        cli.post("/api/v1/rls/policies", json={
            "workspace_id": "test-ws", "policies": {"viewer": ["channel = 'search'"]}})
        cli.put("/api/v1/ui/layout", json={"workspace_id": "test-ws",
                                           "path": "/p", "order": ["a"]})
        cli.post("/api/v1/alerts/rules", json={
            "workspace_id": "test-ws", "name": "r", "metric": "ga4_sessions",
            "op": "gt", "threshold": 1})
        cli.post("/api/v1/comments", json={
            "workspace_id": "test-ws", "target_type": "insight",
            "target_id": "i1", "body": "b"})
        logs = cli.get("/api/v1/audit/admin",
                       params={"workspace_id": "test-ws"}).json()["logs"]
        actions = {x["action"] for x in logs}
        assert {"rls.update", "layout.save", "alert_rule.upsert",
                "comment.add"} <= actions
        csv = cli.get("/api/v1/audit/admin",
                      params={"workspace_id": "test-ws", "format": "csv"})
        assert csv.status_code == 200 and "text/csv" in csv.headers["content-type"]
        assert len(csv.text.strip().splitlines()) >= 2
        filtered = cli.get("/api/v1/audit/admin", params={
            "workspace_id": "test-ws", "action": "rls.update"}).json()["logs"]
        assert all(x["action"] == "rls.update" for x in filtered)
        page = cli.get("/console/audit", params={"workspace_id": "test-ws"})
        assert page.status_code == 200 and "rls.update" in page.text

    def test_member_role_change_audited(self, env):
        import asyncio
        from fastapi.testclient import TestClient
        from insflow.server.app import app
        from insflow.core.accounts import AccountManager

        async def _register():
            await AccountManager("test-ws").register("u@test.com", "password123",
                                                     "U", "T")
        asyncio.get_event_loop().run_until_complete(_register())
        cli = TestClient(app)
        r = cli.post("/api/v1/members/role", json={
            "workspace_id": "test-ws", "email": "u@test.com", "role": "viewer"})
        assert r.status_code == 200 and r.json()["previous"] == "owner"
        assert cli.post("/api/v1/members/role", json={
            "workspace_id": "test-ws", "email": "u@test.com",
            "role": "superuser"}).status_code == 400
        assert cli.post("/api/v1/members/role", json={
            "workspace_id": "test-ws", "email": "nobody@test.com",
            "role": "viewer"}).status_code == 404
        logs = cli.get("/api/v1/audit/admin",
                       params={"workspace_id": "test-ws"}).json()["logs"]
        assert any(x["action"] == "member.role_change" for x in logs)
        assert logs[0]["detail"]["to"] == "viewer"


class TestGeoChoropleth:
    def test_parse_and_project(self):
        parsed = geo_mod.parse_geojson(json.dumps(TINY_GEOJSON))
        assert parsed["count"] == 2                      # 无 name 的要素被丢弃
        bounds = geo_mod.bounds_of(parsed["features"])
        assert bounds == (100, 30, 120, 40)
        path = geo_mod.project(parsed["features"][0]["geometry"], bounds, 100, 100)
        assert path.startswith("M") and path.endswith("Z")

    def test_rejects_bad_input(self):
        for bad in ["not json", "{}", json.dumps({"type": "GeometryCollection"})]:
            with pytest.raises(geo_mod.GeoError):
                geo_mod.parse_geojson(bad)
        with pytest.raises(geo_mod.GeoError):
            geo_mod.parse_geojson('{"type":"FeatureCollection","features":[]}')

    def test_custom_name_key(self):
        doc = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"zh": "广东"},
             "geometry": {"type": "Polygon",
                          "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}}]}
        assert geo_mod.parse_geojson(json.dumps(doc), "zh")["features"][0]["label"] == "广东"

    def test_choropleth_render(self):
        parsed = geo_mod.parse_geojson(json.dumps(TINY_GEOJSON))
        svg = c.choropleth(parsed, [("甲区", 80)], unit=" 次",
                           label="测试", filter_dim="province")
        assert "<svg" in svg and 'role="img"' in svg
        assert svg.count('class="geo"') == 2
        assert 'data-cf="province:甲区"' in svg
        assert 'data-cf="province:乙区"' not in svg        # 无数据区域不可联动
        assert "1 个有数据" in svg
        assert "无数据" in svg                            # 未匹配区域显式标注

    def test_import_api_and_page_switch(self, env):
        import asyncio
        from fastapi.testclient import TestClient
        from insflow.server.app import app
        from insflow.engine.demo import DemoSeeder

        async def _seed():
            await DemoSeeder("test-ws", days=20).seed()
        asyncio.get_event_loop().run_until_complete(_seed())
        cli = TestClient(app)
        r = cli.post("/api/v1/geo/datasets", json={
            "workspace_id": "test-ws", "name": "demo-regions",
            "content": json.dumps({"type": "FeatureCollection", "features": [
                {"type": "Feature", "properties": {"name": "广东"},
                 "geometry": {"type": "Polygon",
                              "coordinates": [[[110, 20], [118, 20], [118, 26],
                                               [110, 26], [110, 20]]]}},
                {"type": "Feature", "properties": {"name": "北京"},
                 "geometry": {"type": "Polygon",
                              "coordinates": [[[115, 38], [118, 38], [118, 41],
                                               [115, 41], [115, 38]]]}}]})})
        assert r.status_code == 200 and r.json()["count"] == 2
        assert cli.post("/api/v1/geo/datasets", json={
            "workspace_id": "test-ws", "name": "bad",
            "content": "{}"}).status_code == 400
        listed = cli.get("/api/v1/geo/datasets",
                         params={"workspace_id": "test-ws"}).json()["datasets"]
        assert "demo-regions" in listed
        page = cli.get("/console/cockpit/traffic",
                       params={"workspace_id": "test-ws", "days": 20})
        assert page.status_code == 200
        assert "精确边界 GeoJSON" in page.text and 'class="geo"' in page.text
        logs = cli.get("/api/v1/audit/admin",
                       params={"workspace_id": "test-ws"}).json()["logs"]
        assert any(x["action"] == "geo.dataset_import" for x in logs)

    def test_dataset_missing_falls_back_to_grid(self, env):
        import asyncio
        from fastapi.testclient import TestClient
        from insflow.server.app import app
        from insflow.engine.demo import DemoSeeder

        async def _seed():
            await DemoSeeder("test-ws", days=20).seed()
        asyncio.get_event_loop().run_until_complete(_seed())
        page = TestClient(app).get("/console/cockpit/traffic",
                                   params={"workspace_id": "test-ws", "days": 20})
        assert page.status_code == 200
        assert "精确边界 GeoJSON" not in page.text
        assert 'class="cell"' in page.text                 # 回退网格地图


class TestBenchAndSchedules:
    def test_bench_reports_metrics(self, env):
        import asyncio
        from insflow.engine.bench import run_bench

        async def _run():
            return await run_bench(monitors=20, insights=200, metrics=500, rounds=1)

        rep = asyncio.get_event_loop().run_until_complete(_run())
        assert rep["dataset"]["monitors"] == 20
        assert rep["write"]["rows_per_sec"] > 0
        assert "insights.list(200)" in rep["queries"]
        assert "metrics.series(30d)" in rep["queries"]
        assert "metrics.series(30d, rollup)" in rep["queries"]
        assert rep["queries"]["insights.list(200)"]["p95_ms"] >= 0

    def test_schedules_registered(self):
        src = open("insflow/core/bootstrap.py", encoding="utf-8").read()
        for job in ("digest.daily", "alerts.flush", "token.audit", "alerts.hourly",
                    "rollup.daily"):
            assert job in src, job
        assert "OAuth 授权即将到期" in src       # 临期主动预警

    def test_bench_cli_registered(self):
        from click.testing import CliRunner
        from insflow.cli import main
        res = CliRunner().invoke(main, ["bench", "--help"])
        assert res.exit_code == 0 and "--monitors" in res.output


class TestSchedulerHandlers:
    """回归：调度器以 handler(payload) 调用，所有 handler 必须接受该参数

    静态校验（不启动调度器，避免测试间状态串扰）。
    """

    def test_all_handlers_accept_payload(self):
        import inspect
        import re

        from insflow.core import bootstrap as bs
        src = inspect.getsource(bs)
        names = re.findall(r"async def (_[a-z_]+)\(([^)]*)\)", src)
        assert names, "未找到 handler"
        bad = [f"{n}({args})" for n, args in names if "payload" not in args]
        assert not bad, f"handler 缺少 payload 参数（调度器会传参）：{bad}"
        # 关键任务必须注册
        for job in ("alerts.hourly", "digest.daily", "alerts.flush", "rollup.daily",
                    "token.audit"):
            assert job in src
