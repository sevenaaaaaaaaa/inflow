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


class TestSemanticsLayer:
    """语义层：计算字段（派生指标）+ 表计算 + 血缘"""

    def test_expression_safe_eval(self):
        from insflow.engine.semantics import SemanticError, evaluate, refs, tokenize
        assert evaluate("ga4_conversions / ga4_sessions * 100",
                        {"ga4_conversions": 21, "ga4_sessions": 1000}) == 2.1
        assert evaluate("-3 + (10)", {}) == 7.0
        assert evaluate("1 / 0", {}) == 0.0            # 除零安全
        assert refs("a / b * 100") == ["a", "b"]
        # 无 eval：函数调用/属性访问都进不了计算
        for bad in ["os.system('rm -rf /')", "__import__('os')", "a; b"]:
            with pytest.raises(SemanticError):
                evaluate(bad, {"a": 1})
        assert [v for k, v in tokenize("a+b")] == ["a", "+", "b"]

    def test_table_calcs(self):
        from insflow.engine.semantics import SemanticError, table_calc
        assert table_calc([10, 12, 9], "mom") == [0.0, 0.2, -0.25]
        assert table_calc([1, 2, 3], "cum") == [1.0, 3.0, 6.0]
        assert table_calc([5, 7, 6], "diff") == [0.0, 2.0, -1.0]
        assert table_calc([1, 2, 3, 4], "rolling", window=2) == [1.0, 1.5, 2.5, 3.5]
        assert table_calc([1, 3], "share") == [0.25, 0.75]
        assert table_calc([1, 2], "none") == [1.0, 2.0]
        with pytest.raises(SemanticError):
            table_calc([1, 2], "bogus")

    def test_resolve_derived_and_lineage(self, env):
        import asyncio
        from insflow.engine.demo import DemoSeeder
        from insflow.engine.semantics import resolve_metric

        async def _run():
            store = env["store"]
            await DemoSeeder("test-ws", days=30).seed()
            await store.upsert_metric_def("test-ws", "conversion_rate",
                                         label="转化率",
                                         expr="ga4_conversions / ga4_sessions * 100",
                                         unit="%", owner="growth")
            resolved = await resolve_metric("test-ws", "conversion_rate", days=30)
            nested = await store.upsert_metric_def(
                "test-ws", "cr_doubled", expr="conversion_rate * 2", unit="%")
            doubled = await resolve_metric("test-ws", "cr_doubled", days=30)
            return resolved, doubled, nested

        resolved, doubled, nested = asyncio.get_event_loop().run_until_complete(_run())
        assert resolved["kind"] == "derived" and len(resolved["series"]) > 5
        assert 0 < resolved["value"] < 100                 # 比率量级合理
        deps = {d["metric"] for d in resolved["lineage"]["depends_on"]}
        assert deps == {"ga4_conversions", "ga4_sessions"}
        assert doubled["kind"] == "derived"                # 派生套派生
        assert any(d["metric"] == "conversion_rate"
                   for d in doubled["lineage"]["depends_on"])
        assert nested["version"] == 1

    def test_cycle_detection(self, env):
        import asyncio
        from insflow.engine.semantics import SemanticError, resolve_metric

        async def _run():
            store = env["store"]
            await store.upsert_metric_def("test-ws", "a", expr="b + 1")
            await store.upsert_metric_def("test-ws", "b", expr="a + 1")
            try:
                await resolve_metric("test-ws", "a", days=7)
                return None
            except SemanticError as e:
                return str(e)

        msg = asyncio.get_event_loop().run_until_complete(_run())
        assert msg and "循环依赖" in msg

    def test_def_api_validates_expr_and_explore_renders(self, env):
        import asyncio
        from fastapi.testclient import TestClient
        from insflow.engine.demo import DemoSeeder
        from insflow.server.app import app

        asyncio.get_event_loop().run_until_complete(
            DemoSeeder("test-ws", days=30).seed())
        cli = TestClient(app)
        assert cli.post("/api/v1/metrics/defs", json={
            "workspace_id": "test-ws", "name": "bad", "expr": "a +"}).status_code == 400
        assert cli.post("/api/v1/metrics/defs", json={
            "workspace_id": "test-ws", "name": "cvr", "label": "转化率",
            "expr": "ga4_conversions / ga4_sessions * 100", "unit": "%"}).status_code == 200
        r = cli.post("/api/v1/semantics/resolve", json={
            "workspace_id": "test-ws", "metric": "cvr", "days": 30})
        assert r.status_code == 200 and r.json()["kind"] == "derived"
        assert cli.post("/api/v1/semantics/calc", json={
            "workspace_id": "test-ws", "metric": "ga4_sessions", "mode": "mom",
            "days": 30}).json()["values"]
        assert cli.post("/api/v1/semantics/calc", json={
            "workspace_id": "test-ws", "metric": "ga4_sessions",
            "mode": "bogus"}).status_code == 400
        lineage = cli.get("/api/v1/semantics/lineage", params={
            "workspace_id": "test-ws", "metric": "cvr"}).json()
        assert lineage["kind"] == "derived" and lineage["depends_on"]
        page = cli.get("/console/explore", params={
            "workspace_id": "test-ws", "metric": "cvr", "days": 30})
        assert page.status_code == 200
        assert "派生指标" in page.text and "血缘" in page.text
        calc_page = cli.get("/console/explore", params={
            "workspace_id": "test-ws", "metric": "ga4_sessions", "calc": "cum",
            "days": 30})
        assert calc_page.status_code == 200 and "表计算" in calc_page.text


class TestNewCharts:
    def test_waterfall_treemap_calendar_candlestick(self):
        wf = c.waterfall([("涨价", 120), ("流失", -45)], start=1000, unit=" 元")
        assert "瀑布图" in wf and wf.count('class="wf"') == 3      # 2 因子 + 合计
        tm = c.treemap([("search", 380), ("social", 220)], filter_dim="channel")
        assert tm.count('class="tm"') == 2 and 'data-cf="channel:search"' in tm
        assert "树图" in tm
        cal = c.calendar_heatmap([("2026-09-%02d" % d, d % 7) for d in range(1, 29)])
        assert "日历热力" in cal and cal.count('class="cell"') >= 28
        k = c.candlestick([("09-01", 10, 12, 9, 11), ("09-02", 11, 13, 10, 12)])
        assert "K 线" in k and k.count('class="k"') == 2
        for svg in (wf, tm, cal, k):
            assert 'role="img"' in svg and "aria-label" in svg and "data-tip" in svg

    def test_empty_inputs_are_placeholders(self):
        assert "暂无数据" in c.waterfall([])
        assert "暂无数据" in c.treemap([])
        assert "暂无数据" in c.calendar_heatmap([])
        assert "暂无数据" in c.candlestick([])

    def test_explore_page_exposes_new_types(self, env):
        import asyncio
        from fastapi.testclient import TestClient
        from insflow.engine.demo import DemoSeeder
        from insflow.server.app import app

        asyncio.get_event_loop().run_until_complete(
            DemoSeeder("test-ws", days=30).seed())
        cli = TestClient(app)
        for ct, marker in (("treemap", 'class="tm"'), ("waterfall", 'class="wf"'),
                           ("calendar", 'class="cell"'), ("candlestick", 'class="k"')):
            r = cli.get("/console/explore", params={
                "workspace_id": "test-ws", "metric": "ga4_sessions",
                "days": 30, "chart": ct})
            assert r.status_code == 200, ct
            assert marker in r.text, ct


class TestScimAndWatermark:
    def test_scim_flow(self, env, monkeypatch):
        import asyncio
        from fastapi.testclient import TestClient
        from insflow.server.app import app
        monkeypatch.setenv("INSFLOW_SCIM_TOKEN", "tok-1")
        cli = TestClient(app)
        H = {"Authorization": "Bearer tok-1"}
        assert cli.get("/scim/v2/Users", params={
            "workspace_id": "test-ws"}).status_code == 401          # fail-closed
        assert cli.get("/scim/v2/Users", params={"workspace_id": "test-ws"},
                       headers={"Authorization": "Bearer nope"}).status_code == 401
        cfg = cli.get("/scim/v2/ServiceProviderConfig").json()
        assert cfg["patch"]["supported"] is True
        r = cli.post("/scim/v2/Users?workspace_id=test-ws", headers=H, json={
            "userName": "scim@test.com", "name": {"formatted": "SCIM"},
            "role": "analyst"})
        assert r.status_code == 200
        uid, body = r.json()["id"], r.json()
        assert body["role"] == "analyst" and body["active"] is True
        # 幂等（IdP 重试）
        again = cli.post("/scim/v2/Users?workspace_id=test-ws", headers=H,
                         json={"userName": "scim@test.com"})
        assert again.json()["id"] == uid
        # 不会因为 SCIM 而新建工作区
        async def _count():
            return len(await env["store"].list_workspaces())
        assert asyncio.get_event_loop().run_until_complete(_count()) == 1
        assert cli.get("/scim/v2/Users", params={
            "workspace_id": "test-ws", "filter": 'userName eq "scim@test.com"'},
            headers=H).json()["totalResults"] == 1
        assert cli.get("/scim/v2/Users", params={
            "workspace_id": "test-ws", "filter": 'x co "y"'}, headers=H).status_code == 400
        patched = cli.patch(f"/scim/v2/Users/{uid}?workspace_id=test-ws", headers=H,
                            json={"Operations": [{"op": "replace", "path": "active",
                                                  "value": False}]}).json()
        assert patched["active"] is False
        assert cli.patch(f"/scim/v2/Users/{uid}?workspace_id=test-ws", headers=H,
                         json={"Operations": [{"path": "role",
                                               "value": "root"}]}).status_code == 400
        assert cli.delete(f"/scim/v2/Users/{uid}?workspace_id=test-ws",
                          headers=H).status_code == 204
        groups = cli.get("/scim/v2/Groups", params={"workspace_id": "test-ws"},
                         headers=H).json()["Resources"]
        assert {g["displayName"] for g in groups} == {"owner", "admin", "analyst",
                                                     "viewer"}
        logs = cli.get("/api/v1/audit/admin",
                       params={"workspace_id": "test-ws"}).json()["logs"]
        assert any(x["action"].startswith("scim.") for x in logs)

    def test_group_role_mapping(self, monkeypatch):
        import os
        from insflow.engine.sso import group_role_map, role_from_groups
        monkeypatch.setenv("INSFLOW_OIDC_GROUP_ROLE_MAP",
                           '{"growth":"analyst","ops-leads":"admin","bad":"root"}')
        assert group_role_map() == {"growth": "analyst", "ops-leads": "admin"}
        assert role_from_groups([]) == "analyst"                  # 默认
        assert role_from_groups(["ops-leads"]) == "admin"
        assert role_from_groups(["growth", "ops-leads"]) == "admin"   # 取更高
        assert role_from_groups(["growth"],
                                {"oidc_group_role_map": {"growth": "viewer"}}) == "viewer"
        os.environ.pop("INSFLOW_OIDC_GROUP_ROLE_MAP", None)

    def test_watermark_and_access_audit(self, env, monkeypatch):
        import asyncio
        import io as _io
        import zipfile
        from fastapi.testclient import TestClient
        from insflow.engine.demo import DemoSeeder
        from insflow.engine.watermark import (csv_with_watermark, header,
                                              xlsx_watermark_sheet)
        from insflow.server.app import app

        assert "导出水印" in csv_with_watermark("a,b\n1,2\n", "u@x.com", "某公司")
        assert xlsx_watermark_sheet("u@x.com", "某公司")[0] == "导出信息"
        assert "%E5%AF%BC%E5%87%BA" in header("u@x.com", "某公司")["X-Export-Watermark"]
        asyncio.get_event_loop().run_until_complete(
            DemoSeeder("test-ws", days=10).seed())

        async def _brand():
            store = env["store"]
            ws = await store.get_workspace("test-ws")
            settings = dict(ws.settings_json or {})
            settings["branding"] = {"company": "某公司"}
            ws.settings_json = settings
            await store.update_workspace(ws)
        asyncio.get_event_loop().run_until_complete(_brand())
        cli = TestClient(app)
        x = cli.get("/api/v1/export/xlsx", params={
            "workspace_id": "test-ws", "panel": "cockpit:traffic", "days": 10})
        assert x.status_code == 200
        assert x.headers.get("x-export-watermark")
        wb = zipfile.ZipFile(_io.BytesIO(x.content)).read("xl/workbook.xml").decode()
        assert "导出信息" in wb
        csv = cli.get("/api/v1/audit/admin", params={
            "workspace_id": "test-ws", "format": "csv"})
        assert csv.text.startswith("# 导出水印")
        logs = cli.get("/api/v1/audit/admin",
                       params={"workspace_id": "test-ws"}).json()["logs"]
        assert any(x["action"] == "access.export_xlsx" for x in logs)
        assert any(x["action"] == "access.export_csv" for x in logs)


class TestBuiltinWorldMap:
    """内置世界边界（Natural Earth 110m，公有领域）+ 自动选图"""

    def test_builtin_topojson_decodes(self):
        from insflow.engine.geo import bounds_of, builtin_dataset
        ds = builtin_dataset()
        assert ds and ds["builtin"] and ds["count"] > 150
        labels = {f["label"] for f in ds["features"]}
        assert "China" in labels and "United States of America" in labels
        assert "Antarctica" not in labels                    # 默认剔除
        min_lon, min_lat, max_lon, max_lat = bounds_of(ds["features"])
        assert -181 <= min_lon <= -170 and 179 <= max_lon <= 181   # 经度已折返
        # 去掉南极洲后纬度范围仍在南半球边缘（如法属南部领地），但不再压到 -90
        assert -100 < min_lat < -50 and 70 < max_lat < 90

    def test_builtin_is_compact(self):
        from insflow.engine.geo import builtin_dataset
        from insflow.viz import charts as c
        svg = c.choropleth(builtin_dataset(), [("China", 10)])
        assert len(svg) < 260_000                     # 抽稀+降精度后应有界

    def test_parse_topojson_direct(self):
        from insflow.engine.geo import GeoError, parse_topojson
        doc = {"type": "Topology", "transform": {"scale": [1, 1], "translate": [0, 0]},
               "arcs": [[[0, 0], [10, 0], [0, 10], [-10, 0], [0, -10]]],
               "objects": {"countries": {"type": "GeometryCollection", "geometries": [
                   {"type": "Polygon", "arcs": [[0]], "properties": {"name": "方块"}}]}}}
        parsed = parse_topojson(doc)
        assert parsed["count"] == 1 and parsed["features"][0]["label"] == "方块"
        assert parsed["features"][0]["geometry"]["type"] == "Polygon"
        with pytest.raises(GeoError):
            parse_topojson({"type": "FeatureCollection"})

    def test_alias_matching_picks_dataset(self):
        from insflow.engine.geo import builtin_dataset, match_score, pick_dataset
        ds = builtin_dataset()
        assert match_score(ds, ["美国", "中国", "德国"]) == 3    # 中文别名
        assert match_score(ds, ["广东", "北京"]) == 0            # 省份不匹配国家图
        name, picked = pick_dataset({}, {"world": ds}, ["美国"])
        assert name == "world" and picked is ds
        assert pick_dataset({}, {"world": ds}, ["广东"]) == ("", None)

    def test_list_datasets_includes_builtin(self):
        from insflow.engine.geo import list_datasets
        ds = list_datasets({"geo_datasets": {"mine": {"source": "upload"}}})
        assert "world" in ds and ds["world"]["builtin"] is True
        assert "mine" in ds


class TestTrueOhlc:
    def test_ohlc_from_multiple_samples(self, env):
        import asyncio

        async def _run():
            store = env["store"]
            samples = [("2026-09-10T01:00:00+00:00", 100.0),
                       ("2026-09-10T09:00:00+00:00", 130.0),
                       ("2026-09-10T21:00:00+00:00", 90.0)]
            for i, (ts, v) in enumerate(samples):
                await store._execute(
                    """INSERT INTO metrics (id, workspace_id, entity_type, entity_id,
                       metric, value, dim_json, ts) VALUES (?, 'test-ws', 'competitor',
                       'c1', 'competitor_price_ohlc', ?, '{}', ?)""",
                    (f"o{i}", v, ts))
            await store._db.commit()
            return await store.metric_ohlc("test-ws", "competitor_price_ohlc", days=3650,
                                           entity_id="c1")

        rows = asyncio.get_event_loop().run_until_complete(_run())
        assert len(rows) == 1
        bar = rows[0]
        assert (bar["open"], bar["high"], bar["low"], bar["close"]) == (100.0, 130.0,
                                                                       90.0, 90.0)
        assert bar["n"] == 3 and bar["first_ts"] < bar["last_ts"]

    def test_ohlc_buckets_and_single_sample(self, env):
        import asyncio

        async def _run():
            store = env["store"]
            for day, v in (("11", 10.0), ("12", 12.0), ("12", 8.0)):
                await store._execute(
                    """INSERT INTO metrics (id, workspace_id, entity_type, entity_id,
                       metric, value, dim_json, ts) VALUES (?, 'test-ws', 'site', 'x',
                       'price', ?, '{}', ?)""",
                    (f"p{day}{v}", v, f"2026-09-{day}T05:00:00+00:00"))
            await store._db.commit()
            return await store.metric_ohlc("test-ws", "price", days=3650)

        bars = asyncio.get_event_loop().run_until_complete(_run())
        assert [b["bucket"] for b in bars] == ["2026-09-11", "2026-09-12"]
        assert bars[1]["open"] == 12.0 and bars[1]["close"] == 8.0
        assert bars[1]["high"] == 12.0 and bars[1]["low"] == 8.0

    def test_demo_price_ohlc_and_cockpit(self, env):
        import asyncio
        from fastapi.testclient import TestClient
        from insflow.engine.demo import DemoSeeder
        from insflow.server.app import app

        asyncio.get_event_loop().run_until_complete(
            DemoSeeder("test-ws", days=12).seed())
        cli = TestClient(app)
        page = cli.get("/console/cockpit/competitor", params={
            "workspace_id": "test-ws", "days": 12})
        assert page.status_code == 200
        assert "竞品价格波动" in page.text and 'class="k"' in page.text
        ex = cli.get("/console/explore", params={
            "workspace_id": "test-ws", "metric": "competitor_price_ohlc",
            "days": 12, "chart": "candlestick"})
        assert ex.status_code == 200 and "真实 OHLC" in ex.text


class TestScatterSamplingAndGL:
    def test_large_scatter_is_sampled_for_transport(self):
        import html
        import json
        import re
        big = c.scatter([(i, i % 50, f"p{i}") for i in range(100_000)])
        meta = json.loads(html.unescape(re.search(r"data-chart='([^']+)'", big).group(1)))
        assert meta["kind"] == "scatter"
        assert len(meta["points"]) == 20_000 and meta["sampled"] is True
        assert meta["sampled_from"] == 100_000
        assert "抽样" in big and 'role="img"' in big

    def test_small_scatter_not_sampled(self):
        import html
        import json
        import re
        small = c.scatter([(i, i, f"p{i}") for i in range(1200)],
                          canvas_threshold=800, max_points=20000)
        meta = json.loads(html.unescape(re.search(r"data-chart='([^']+)'", small).group(1)))
        assert meta["sampled"] is False and len(meta["points"]) == 1200

    def test_gl_renderer_hooks_present(self):
        base = open("insflow/web/templates/base.html", encoding="utf-8").read()
        for token in ("ifDrawGL", "ifShouldGL", "ifGetGL", "ifProgram",
                      "VERTEX_SHADER", "FRAGMENT_SHADER", "gl.POINTS", "gl.LINES",
                      "preserveDrawingBuffer", "data-renderer", "'webgl'"):
            assert token in base, token
        assert "threshold_points: 8000" in base and "threshold_line: 40000" in base


class TestMobileAndPwa:
    def test_mobile_page(self, env):
        import asyncio
        from fastapi.testclient import TestClient
        from insflow.engine.demo import DemoSeeder
        from insflow.server.app import app

        asyncio.get_event_loop().run_until_complete(
            DemoSeeder("test-ws", days=7).seed())
        page = TestClient(app).get("/console/m", params={"workspace_id": "test-ws"})
        assert page.status_code == 200
        assert "移动端快照" in page.text and "card kpi" in page.text
        assert "__ifMobileSnapshot" in page.text          # 离线兜底用快照
        assert "ifInstall" in page.text                   # 安装引导
        assert "原生应用商店 App 不在本项目范围" in page.text

    def test_manifest_and_sw_mobile(self):
        from fastapi.testclient import TestClient
        from insflow.server.app import app
        cli = TestClient(app)
        m = cli.get("/console/manifest.webmanifest").json()
        assert m["start_url"].endswith("/console/m")
        assert len(m["shortcuts"]) >= 3
        sw = cli.get("/console/sw.js").text
        assert "insflow-m-snapshot" in sw and "/console/m" in sw
        assert "network" not in sw.split("snapshot")[0][-200:] or "fetch(req)" in sw
        assert "shell-v2" in sw


class TestWebglBehavior:
    """WebGL 渲染器行为验证（提取线上同源 JS，在 Node 里跑 mock GL）

    无浏览器依赖：用 mock 的 WebGL 上下文断言「GPU 路径被调用 / 无 GL 时回退 2D」。
    Node 不可用时跳过（CI 无 node 也不阻塞）。
    """

    def _node(self):
        import shutil
        return shutil.which("node")

    def test_gl_path_and_fallback(self, tmp_path):
        import shutil
        import subprocess
        node = self._node()
        if not node:
            pytest.skip("未安装 node")
        src = open("insflow/web/templates/base.html", encoding="utf-8").read()
        start = src.index("var IFGL =")
        end = src.index("function ifDrawChart(")
        block = src[start:end]
        harness = """
var calls = {drawArrays: [], clear: 0};
function mockGL(){ return {
  canvas:{width:1440,height:440},
  VERTEX_SHADER:1,FRAGMENT_SHADER:2,ARRAY_BUFFER:3,STATIC_DRAW:4,COMPILE_STATUS:5,
  LINK_STATUS:6,POINTS:7,LINES:8,COLOR_BUFFER_BIT:9,
  clearColor(){}, clear(){calls.clear++;}, viewport(){},
  createShader(){return {};}, shaderSource(){}, compileShader(){},
  getShaderParameter(){return true;}, createProgram(){return {};}, attachShader(){},
  linkProgram(){}, getProgramParameter(){return true;}, useProgram(){},
  getAttribLocation(){return 0;}, enableVertexAttribArray(){}, vertexAttribPointer(){},
  createBuffer(){return {};}, bindBuffer(){}, bufferData(){}, deleteBuffer(){},
  drawArrays(mode, first, count){calls.drawArrays.push([mode, count]);},
  getUniformLocation(){return null;} }; }
var document = {documentElement:{}};
var getComputedStyle = function(){ return {getPropertyValue:function(){ return 'var(--accent)'; }}; };
var window = {};
__BLOCK__
var meta = {kind:'scatter', x_max:100, y_max:100, plot:[44,14,660,180], points:[]};
for (var i=0;i<9000;i++) meta.points.push([i%100,(i*7)%100,'p'+i]);
console.log('shouldGL=' + ifShouldGL(meta));
console.log('fallback=' + ifDrawGL({width:10,height:10,getContext(){return null;}}, meta, 720, 220));
var ok = ifDrawGL({width:1440,height:440,getContext(){return mockGL();}}, meta, 720, 220);
console.log('drawGL=' + ok + ' points=' + JSON.stringify(calls.drawArrays) + ' clear=' + calls.clear);
console.log('small=' + ifShouldGL({kind:'scatter', points:new Array(100).fill([0,0,''])}));
""".replace("__BLOCK__", block)
        f = tmp_path / "gl.js"
        f.write_text(harness, encoding="utf-8")
        out = subprocess.run([node, str(f)], capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr[-500:]
        # 注意：'drawGL=true points=…' 里含空格，先按前缀切分再单独比对
        lines = dict(l.split("=", 1) for l in out.stdout.strip().splitlines())
        draw = lines.pop("drawGL", "")
        assert lines["shouldGL"] == "true"
        assert lines["fallback"] == "false"          # 无 GL → 回退 2D
        assert draw.startswith("true")               # GPU 路径成功
        assert "[[7,9000]]" in draw                  # POINTS × 9000
        assert "clear=1" in draw
        assert lines["small"] == "false"


class TestGeoDimSelection:
    """地域维度自动识别：province → country → region（此前硬编码 province，
    导致国家维度数据永远不出现）"""

    def _seed(self, env, workspace_id: str, dim: str, rows: list):
        import asyncio

        async def _run():
            store = env["store"]
            await store.create_workspace(Workspace(id=workspace_id, name=workspace_id))
            for i, (key, value) in enumerate(rows):
                await store._execute(
                    """INSERT INTO metrics (id, workspace_id, entity_type, entity_id,
                       metric, value, dim_json, ts) VALUES (?, ?, 'site', 'main',
                       'ga4_sessions', ?, ?, '2026-09-10T00:00:00+00:00')""",
                    (f"{workspace_id}-{i}", workspace_id, value,
                     json.dumps({dim: key}, ensure_ascii=False)))
            await store._db.commit()
        asyncio.get_event_loop().run_until_complete(_run())

    def test_country_dim_uses_builtin_world_map(self, env):
        import asyncio
        from fastapi.testclient import TestClient
        from insflow.server.app import app

        self._seed(env, "geo-country", "country",
                   [("United States of America", 40), ("China", 30), ("Germany", 12)])
        r = TestClient(app).get("/console/cockpit/traffic",
                                params={"workspace_id": "geo-country", "days": 30})
        assert r.status_code == 200
        assert 'class="geo"' in r.text                      # 内置世界图
        assert 'data-cf="country:China"' in r.text           # 联动维度正确
        assert 'class="cell"' not in r.text

    def test_province_dim_prefers_grid_over_world(self, env):
        from fastapi.testclient import TestClient
        from insflow.server.app import app
        self._seed(env, "geo-province", "province",
                   [("广东", 20), ("北京", 10)])
        r = TestClient(app).get("/console/cockpit/traffic",
                                params={"workspace_id": "geo-province", "days": 30})
        assert r.status_code == 200
        assert 'class="cell"' in r.text                      # 网格地图（世界图不匹配省份）
        assert 'class="geo"' not in r.text

    def test_region_dim_fallback(self, env):
        from fastapi.testclient import TestClient
        from insflow.server.app import app
        self._seed(env, "geo-region", "region", [("APAC", 5), ("EMEA", 3)])
        r = TestClient(app).get("/console/cockpit/traffic",
                                params={"workspace_id": "geo-region", "days": 30})
        assert r.status_code == 200
        assert 'class="cell"' in r.text                      # 无匹配数据集 → 网格
