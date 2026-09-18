"""Batch1 测试：图例开关 / 动画 / 桑基·网格地图·箱线 / Canvas 大数据量 /
异常带（z-score 留一法）/ 季节性预测 / 维度感知幂等窗口键（对齐 docs/10 P1-P2）"""

import html

import pytest

import insflow.core.files as files_mod
from insflow.core.entities import Workspace
from insflow.core.store import Store, dim_window_key, reset_store
from insflow.viz import charts as c
from insflow.viz.frame import datapanel
from tests._ui_source import ui_source

BASE_HTML = "insflow/web/templates/base.html"


class TestLegendToggle:
    def test_legend_is_interactive(self):
        svg = c.line_chart([{"name": "本期", "values": [1, 2, 3]}],
                           ["a", "b", "c"], compare={"name": "上期",
                                                     "series": [{"name": "上期",
                                                                 "values": [1, 1, 2]}]})
        assert 'class="if-legend"' in svg
        assert "ifToggleSeries" in svg
        assert 'role="button"' in svg and 'aria-pressed="true"' in svg
        assert 'data-series="本期"' in svg          # 序列元素可被开关
        assert 'data-series="上期"' in svg          # 对比序列同样可开关

    def test_legend_js_and_style_present(self):
        src = ui_source()
        assert "function ifToggleSeries" in src
        assert "function ifLegendKey" in src        # 键盘可操作
        assert ".if-legend.off" in src
        assert "aria-pressed" in src


class TestAnimation:
    def test_chart_has_anim_class(self):
        svg = c.line_chart([{"name": "x", "values": [1, 2]}], ["a", "b"])
        assert "if-anim" in svg
        assert "if-anim" not in c.line_chart([{"name": "x", "values": [1, 2]}],
                                             ["a", "b"], anim=False)

    def test_canvas_animation_respects_reduced_motion(self):
        src = ui_source()
        assert "@keyframes ifRise" in src
        assert "function ifAnimate" in src
        assert "prefers-reduced-motion" in src


class TestSankey:
    def test_flows_and_nodes(self):
        svg = c.sankey([("Search", "Signup", 120), ("Social", "Signup", 60),
                        ("Search", "Churn", 30)], unit=" 人")
        assert svg.count('class="flow"') == 3
        assert "Search" in svg and "Signup" in svg
        assert 'aria-label="桑基图' in svg

    def test_empty_is_placeholder(self):
        assert "暂无数据" in c.sankey([])


class TestTileMap:
    def test_china_scope_renders_all_regions(self):
        svg = c.tile_map([("广东", 120), ("北京", 80)], scope="china")
        assert svg.count('class="cell"') == len(c.CHINA_GRID)
        assert "网格地图" in svg and "广东" in svg

    def test_unknown_region_shows_no_data(self):
        svg = c.tile_map([("火星", 1)], scope="china")
        assert "无数据" in svg            # 未覆盖地区不静默变 0

    def test_world_scope(self):
        svg = c.tile_map([("美国", 10)], scope="world")
        assert svg.count('class="cell"') == len(c.WORLD_GRID)


class TestBoxplot:
    def test_quartiles_and_outliers(self):
        svg = c.boxplot([("A", [1, 2, 3, 4, 5, 100]), ("B", [4, 5, 6, 7, 8])],
                        y_label="转化量")
        assert "离群" in svg and "中位" in svg
        assert 'aria-label="箱线图' in svg
        assert c._quantile([1, 2, 3, 4], 0.5) == 2.5
        assert c._quantile([10], 0.9) == 10


class TestCanvasLargeData:
    def test_scatter_switches_to_canvas(self):
        big = c.scatter([(i, i * 2, f"p{i}") for i in range(1000)])
        assert "data-canvas" in big and '"kind": "scatter"' in html.unescape(big)
        assert 'role="img"' in big and "aria-label" in big
        assert c.scatter([(1, 2, "a")]).startswith("<svg")

    def test_heatmap_switches_to_canvas(self):
        big = c.heatmap([f"r{i}" for i in range(40)], [f"c{j}" for j in range(30)],
                        [[0.5] * 30 for _ in range(40)])
        assert "data-canvas" in big and '"kind": "heatmap"' in html.unescape(big)
        assert c.heatmap(["r"], ["c"], [[0.4]]).startswith("<svg")

    def test_canvas_renderers_present(self):
        src = ui_source()
        assert "function ifDrawScatter" in src
        assert "function ifDrawHeatmap" in src
        assert "meta.kind === 'scatter'" in src


class TestAnomalyBand:
    def test_spike_detected_with_leave_one_out(self):
        up, low, bad = c.anomaly_points([1] * 7 + [99])
        assert bad == [7] and len(up) == 8 and len(low) == 8

    def test_flat_series_has_no_anomaly(self):
        assert c.anomaly_points([1] * 10)[2] == []

    def test_chart_marks_anomaly(self):
        svg = c.line_chart([{"name": "x", "values": [1] * 7 + [99]}],
                           [f"d{i}" for i in range(8)], anomaly=True)
        assert 'class="anom"' in svg
        assert '"anomaly"' in html.unescape(svg)
        assert "异常标记" in svg

    def test_band_off_by_default(self):
        svg = c.line_chart([{"name": "x", "values": [1, 2, 3]}], ["a", "b", "c"])
        assert 'class="anom"' not in svg


class TestSeasonalForecast:
    def test_seasonal_restores_cycle(self):
        vals = [10, 20, 30] * 4
        preds, band = c.forecast_series_seasonal(vals, 3)
        assert len(preds) == 3 and len(band) == 3
        assert all(5 < p < 40 for p in preds)      # 复原周期量级

    def test_fallback_when_short(self):
        preds, _ = c.forecast_series_seasonal([1, 2], 3)
        assert preds == []                          # 样本不足 → 不硬预测

    def test_chart_uses_seasonal_when_asked(self):
        vals = ([10, 20, 30] * 4)
        svg = c.line_chart([{"name": "x", "values": vals}],
                           [f"d{i}" for i in range(len(vals))],
                           forecast_periods=3, seasonal=True)
        assert "预测" in svg


class TestDimWindowKey:
    def test_dim_produces_distinct_keys(self):
        ts = "2026-09-17T10:23:00+00:00"
        base = dim_window_key(ts)
        gd = dim_window_key(ts, {"province": "广东"})
        bj = dim_window_key(ts, {"province": "北京"})
        assert base != gd and gd != bj
        assert base.startswith("2026-09-17-10")

    def test_same_dim_same_key(self):
        ts = "2026-09-17T10:23:00+00:00"
        assert dim_window_key(ts, {"channel": "search"}) == dim_window_key(
            ts, {"channel": "search"})


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


class TestDimBreakdownAndCockpit:
    async def test_metric_dim_breakdown(self, env):
        store = env["store"]
        for i, (prov, v) in enumerate([("广东", 10), ("北京", 6), ("广东", 5)]):
            await store._execute(
                """INSERT INTO metrics (id, workspace_id, entity_type, entity_id,
                   metric, value, dim_json, ts) VALUES (?, 'test-ws', 'site', 'main',
                   'ga4_sessions', ?, ?, '2026-09-10T00:00:00+00:00')""",
                (f"m{i}", v, '{"province": "%s"}' % prov))
        await store._db.commit()
        rows = await store.metric_dim_breakdown("test-ws", "ga4_sessions", "province",
                                                days=3650)
        assert rows[0] == {"key": "广东", "value": 15.0, "n": 2}

    async def test_demo_geo_and_traffic_panel(self, env):
        from fastapi.testclient import TestClient

        from insflow.engine.demo import DemoSeeder
        from insflow.server.app import app
        await DemoSeeder("test-ws", days=10).seed()
        store = env["store"]
        rows = await store.metric_dim_breakdown("test-ws", "ga4_sessions", "province",
                                                days=90)
        assert len(rows) >= 5
        r = TestClient(app).get("/console/cockpit/traffic?days=10")
        assert r.status_code == 200
        assert 'class="cell"' in r.text           # 网格地图
        assert "中位" in r.text                    # 箱线
        assert "if-legend" in r.text              # 图例开关


class TestExportXlsxPng:
    def test_xlsx_is_valid_package(self):
        import io as _io
        import zipfile

        from insflow.core.xlsx import write_xlsx
        data = write_xlsx([("趋势", ["时间", "值"], [["d1", 1.5], ["d2", 2]]),
                           ("说明/补充", ["a"], [["x"]])])
        z = zipfile.ZipFile(_io.BytesIO(data))
        assert z.testzip() is None
        names = z.namelist()
        assert "xl/workbook.xml" in names and "xl/worksheets/sheet1.xml" in names
        assert "说明-补充" in z.read("xl/workbook.xml").decode()   # 非法字符已清洗
        assert 't="inlineStr"' in z.read("xl/worksheets/sheet1.xml").decode()

    def test_datapanel_has_xlsx_and_png_buttons(self):
        panel = datapanel("标题", ["a", "b"], [["x", 1]], "<svg></svg>", csv_name="t")
        assert "XLSX" in panel and "spreadsheetml" in panel
        assert "ifExportPNG" in panel

    def test_datapanel_xlsx_size_guard(self):
        big = datapanel("大表", ["a"], [[i] for i in range(6000)], "<svg></svg>")
        assert "spreadsheetml" not in big     # 超限不外链，避免 HTML 膨胀

    def test_export_api(self, env):
        import asyncio

        from fastapi.testclient import TestClient

        from insflow.engine.demo import DemoSeeder
        from insflow.server.app import app

        async def _seed():
            await DemoSeeder("test-ws", days=8).seed()

        asyncio.get_event_loop().run_until_complete(_seed())
        c = TestClient(app)
        r = c.get("/api/v1/export/xlsx", params={"workspace_id": "test-ws",
                                                 "panel": "cockpit:traffic", "days": 8})
        assert r.status_code == 200
        assert "spreadsheetml" in r.headers["content-type"]
        assert "attachment" in r.headers["content-disposition"]
        assert r.content[:2] == b"PK"
        r2 = c.get("/api/v1/export/xlsx", params={"workspace_id": "test-ws",
                                                  "panel": "metric:ga4_sessions"})
        assert r2.status_code == 200
        r3 = c.get("/api/v1/export/xlsx", params={"workspace_id": "test-ws",
                                                  "panel": "nope:x"})
        assert r3.status_code == 400

    def test_png_export_js_present(self):
        src = ui_source()
        for token in ("function ifExportPNG", "function ifInlineSvgVars",
                      "function ifCssVarMap", "function ifExportPageXlsx"):
            assert token in src


class TestBoardEmbed:
    def test_board_token_renders_multi_panel(self, env):
        import asyncio

        from fastapi.testclient import TestClient

        from insflow.engine.demo import DemoSeeder
        from insflow.engine.embed import mint
        from insflow.server.app import app

        async def _seed():
            await DemoSeeder("test-ws", days=10).seed()

        asyncio.get_event_loop().run_until_complete(_seed())
        c = TestClient(app)
        r = c.get("/console/embed", params={"token": mint("test-ws", "board:traffic")})
        assert r.status_code == 200
        assert r.text.count('class="dp"') >= 2       # 多面板
        assert 'class="card kpi"' in r.text          # KPI 行
        assert "即席探索" not in r.text               # 只读
        assert 'role="img"' in r.text
        assert "XLSX" in r.text                      # 嵌入内也能导出数据
        assert "ifExportPNG" not in r.text           # 图片导出留给控制台
        assert "_embed_js" not in r.text

    def test_theme_override(self, env):
        from fastapi.testclient import TestClient

        from insflow.engine.embed import mint
        from insflow.server.app import app
        c = TestClient(app)
        r = c.get("/console/embed", params={"token": mint("test-ws", "board:overview"),
                                            "theme": "dark"})
        assert 'data-theme="dark"' in r.text


class TestCrossFilterLayoutPwa:
    async def _seeded(self, env):
        from insflow.engine.demo import DemoSeeder
        await DemoSeeder("test-ws", days=10).seed()

    def test_filtered_query_is_parameterized(self, env):
        """维度过滤走参数化 SQL（值来自 URL，不能拼接）"""
        import asyncio


        async def _run():
            store = env["store"]
            await store._execute(
                """INSERT INTO metrics (id, workspace_id, entity_type, entity_id,
                   metric, value, dim_json, ts) VALUES ('f1','test-ws','site','main',
                   'ga4_sessions', 5, '{"province": "广东"}', '2026-09-10T00:00:00+00:00')""")
            await store._db.commit()
            hit = await store.metric_total("test-ws", "ga4_sessions", days=3650,
                                           dim_filters={"province": "广东"})
            miss = await store.metric_total("test-ws", "ga4_sessions", days=3650,
                                            dim_filters={"province": "北京"})
            bad_key = await store.metric_total("test-ws", "ga4_sessions", days=3650,
                                               dim_filters={"evil": "' OR 1=1"})
            return hit, miss, bad_key

        hit, miss, bad_key = asyncio.get_event_loop().run_until_complete(_run())
        assert hit == 5 and miss == 0
        assert bad_key == 5          # 非白名单键被忽略（不注入）

    def test_cross_filter_chip_and_attrs(self, env):
        import asyncio

        from fastapi.testclient import TestClient

        from insflow.server.app import app

        asyncio.get_event_loop().run_until_complete(self._seeded(env))
        c = TestClient(app)
        r = c.get("/console/cockpit/traffic", params={"days": 10, "cf.province": "广东"})
        assert r.status_code == 200
        assert "联动筛选" in r.text and "广东" in r.text
        assert "ifClearCrossFilter" in r.text
        plain = c.get("/console/cockpit/traffic", params={"days": 10})
        assert "data-cf=" in plain.text              # 可点击触发联动

    def test_layout_api_roundtrip(self, env):
        from fastapi.testclient import TestClient

        from insflow.server.app import app
        c = TestClient(app)
        assert c.put("/api/v1/ui/layout", json={"workspace_id": "test-ws",
                                                "path": "/console/cockpit/traffic",
                                                "order": ["box", "geo"]}).status_code == 200
        got = c.get("/api/v1/ui/layout", params={"workspace_id": "test-ws",
                                                "path": "/console/cockpit/traffic"}).json()
        assert got["order"] == ["box", "geo"]
        assert c.put("/api/v1/ui/layout", json={"workspace_id": ""}).status_code == 400

    def test_pwa_endpoints(self):
        from fastapi.testclient import TestClient

        from insflow.server.app import app
        c = TestClient(app)
        m = c.get("/console/manifest.webmanifest")
        assert m.status_code == 200 and "Insight Flow" in m.text
        assert "svg" in m.text
        sw = c.get("/console/sw.js")
        assert sw.status_code == 200 and "serviceWorker" not in sw.text
        assert "addEventListener('fetch'" in sw.text
        assert c.get("/console/icon.svg").status_code == 200

    def test_manifest_and_sw_registered_in_html(self):
        src = ui_source()
        assert "/console/manifest.webmanifest" in src
        assert "/console/sw.js" in src
        assert "ifSetupLayout" in src and "draggable" in src
        assert "env(safe-area-inset-bottom)" in src


class TestBatch45Platform:
    """Batch4/5：预聚合、透视、SQL 沙箱、语义层、评论、告警、估算、权限、订阅"""

    def test_rollup_and_long_window(self, env):
        import asyncio

        from insflow.core.rollup import daily_series, rollup, rollup_stats
        from insflow.engine.demo import DemoSeeder

        async def _run():
            await DemoSeeder("test-ws", days=20).seed()
            res = await rollup("test-ws", days=60)
            stats = await rollup_stats("test-ws")
            series = await daily_series("test-ws", "ga4_sessions", days=60)
            return res, stats, series

        res, stats, series = asyncio.get_event_loop().run_until_complete(_run())
        assert res["rows"] > 0 and stats["metrics"] > 0
        assert len(series) > 0 and series[0]["bucket"].count("-") == 2

    def test_pivot_two_dims_and_time(self, env):
        import asyncio

        from insflow.engine.demo import DemoSeeder

        async def _run():
            store = env["store"]
            await DemoSeeder("test-ws", days=20).seed()
            p2 = await store.pivot("test-ws", "ga4_sessions", "province", "channel",
                                   days=20)
            pt = await store.pivot("test-ws", "ga4_sessions", "province", days=20)
            return p2, pt

        p2, pt = asyncio.get_event_loop().run_until_complete(_run())
        assert p2["rows"] and p2["cols"] and p2["dim2"] == "channel"
        assert pt["dim2"] == "time" and len(pt["cols"]) > 3

    def test_sql_sandbox_isolation_and_guards(self, env):
        import asyncio

        from insflow.engine.explore_sql import SqlError, run, validate

        async def _run():
            store = env["store"]
            await store.create_workspace(Workspace(id="other-ws", name="o"))
            for ws, entity in (("test-ws", "A"), ("other-ws", "B")):
                await store._execute(
                    """INSERT INTO metrics (id, workspace_id, entity_type, entity_id,
                       metric, value, dim_json, ts) VALUES (?, ?, 'site', ?, 'ga4_sessions',
                       5, '{}', '2026-09-10T00:00:00+00:00')""",
                    (f"m-{ws}", ws, entity))
            await store._db.commit()
            return await run("test-ws", "SELECT entity_id FROM metrics")

        out = asyncio.get_event_loop().run_until_complete(_run())
        assert [r[0] for r in out["rows"]] == ["A"]      # 跨租户不可见
        with pytest.raises(SqlError):
            validate("DELETE FROM metrics")
        with pytest.raises(SqlError):
            validate("SELECT * FROM users")
        with pytest.raises(SqlError):
            validate("SELECT 1; SELECT 2")

    def test_metric_defs_versioning(self, env):
        import asyncio

        async def _run():
            store = env["store"]
            v1 = await store.upsert_metric_def("test-ws", "cvr", label="转化率",
                                              expr="conversions / sessions * 100",
                                              unit="%", owner="growth")
            v2 = await store.upsert_metric_def("test-ws", "cvr",
                                              expr="conversions / sessions * 1000",
                                              notes="口径调整")
            same = await store.upsert_metric_def("test-ws", "cvr", owner="ops")
            return v1, v2, same

        v1, v2, same = asyncio.get_event_loop().run_until_complete(_run())
        assert v1["version"] == 1
        assert v2["version"] == 2 and v2["notes"] == "口径调整"
        assert same["version"] == 2 and same["owner"] == "ops"   # 仅改责任人不动版本

    def test_comments_and_alerts_and_estimate(self, env):
        import asyncio

        from insflow.engine.alerts import evaluate_workspace, sweep_escalations
        from insflow.engine.demo import DemoSeeder
        from insflow.engine.traffic_estimate import estimate_many

        async def _run():
            store = env["store"]
            await DemoSeeder("test-ws", days=20).seed()
            await store.add_comment("test-ws", "insight", "i1", "复核", "seven")
            comments = await store.list_comments("test-ws")
            await store.upsert_alert_rule("test-ws", {
                "name": "会话过低", "metric": "ga4_sessions", "op": "lt",
                "threshold": 1e12, "window_days": 7, "routes": ["webhook"]})
            fired = await evaluate_workspace("test-ws")
            again = await evaluate_workspace("test-ws")       # 同窗口去重
            est = await estimate_many("test-ws",
                                      ["main-site.com", "competitor-b.com"], 20)
            return comments, fired, again, est

        comments, fired, again, est = asyncio.get_event_loop().run_until_complete(_run())
        assert comments[0]["body"] == "复核"
        assert len(fired) == 1 and fired[0]["insight_id"]
        assert again == []
        assert est[0]["index"] > est[1]["index"]     # 主站指数应高于弱竞品
        assert est[0]["method"] and "不做绝对流量承诺" in "".join(est[0]["method"])
        assert sweep_escalations("test-ws") is not None

    def test_role_matrix_and_entity_allow(self):
        from insflow.engine.permissions import (
            MATRIX,
            PermissionError_,
            allowed,
            entity_allow,
            require,
        )
        assert allowed("owner", "anything.write")
        assert allowed("analyst", "explore.sql") and not allowed("viewer", "explore.sql")
        assert not allowed("viewer", "alert.write")
        assert MATRIX["viewer"] == {"read", "export"}
        with pytest.raises(PermissionError_):
            require("viewer", "explore.sql")
        assert entity_allow({"role_entity_allow": {"viewer": ["A"]}}, "viewer") == ["A"]
        assert entity_allow({"role_entity_allow": {"viewer": []}}, "viewer") == []

    def test_store_honors_entity_allow(self, env):
        import asyncio

        async def _run():
            store = env["store"]
            for i, e in enumerate(["A", "B"]):
                await store._execute(
                    """INSERT INTO metrics (id, workspace_id, entity_type, entity_id,
                       metric, value, dim_json, ts) VALUES (?, 'test-ws', 'site', ?,
                       'ga4_sessions', 5, '{}', '2026-09-10T00:00:00+00:00')""",
                    (f"e{i}", e))
            await store._db.commit()
            total_all = await store.metric_total("test-ws", "ga4_sessions", days=3650)
            total_scoped = await store.metric_total("test-ws", "ga4_sessions", days=3650,
                                                    entity_allow=["A"])
            return total_all, total_scoped

        total_all, total_scoped = asyncio.get_event_loop().run_until_complete(_run())
        assert total_all == 10 and total_scoped == 5

    def test_new_apis(self, env):
        import asyncio

        from fastapi.testclient import TestClient

        from insflow.server.app import app

        asyncio.get_event_loop().run_until_complete(self._seed_demo(env))
        c = TestClient(app)
        assert c.post("/api/v1/explore/pivot", params={
            "workspace_id": "test-ws", "metric": "ga4_sessions",
            "dim": "province", "dim2": "channel", "days": 10}).status_code == 200
        sql = c.post("/api/v1/explore/sql", json={
            "workspace_id": "test-ws",
            "sql": "SELECT metric FROM metrics GROUP BY metric"}).json()
        assert sql["columns"] == ["metric"] and sql["count"] > 0
        assert c.post("/api/v1/explore/sql", json={
            "workspace_id": "test-ws", "sql": "DROP TABLE metrics"}).status_code == 400
        assert c.get("/api/v1/metrics/defs", params={
            "workspace_id": "test-ws"}).json()["defs"] is not None
        assert c.get("/api/v1/vars", params={
            "workspace_id": "test-ws", "metric": "ga4_sessions"}).json()["vars"]
        est = c.get("/api/v1/estimate", params={
            "workspace_id": "test-ws", "domains": "main-site.com"}).json()
        assert est["estimates"][0]["index"] > 0
        assert c.get("/api/v1/auth/me").json()["role"]
        assert c.get("/api/v1/ui/layout/history", params={
            "workspace_id": "test-ws"}).status_code == 200
        r = c.post("/api/v1/subscriptions/metric", json={
            "workspace_id": "test-ws", "name": "图订阅", "metric": "ga4_sessions",
            "channels": ["webhook"], "target": {"webhook_url": "https://example.com/x"}})
        assert r.status_code == 200

    async def _seed_demo(self, env):
        from insflow.engine.demo import DemoSeeder
        await DemoSeeder("test-ws", days=10).seed()

    def test_union_ads_plugin(self):
        import asyncio

        from insflow.collectors.base import CollectContext
        from insflow.collectors.registry import get_registry
        reg = get_registry()
        ids = [p["id"] for p in reg.list_plugins()]
        assert "union-ads" in ids and len(ids) >= 12   # 插件宿主确实在加载
        res = asyncio.run(reg.get("union-ads").collect(CollectContext(
            workspace_id="w", monitor_id="m", config={"payload": [
                {"campaign": "c1", "channel": "affiliate", "spend": 100,
                 "clicks": 20, "conversions": 3, "revenue": 900, "orders": 4}]})))
        metrics = {r["metric"] for r in res.items}
        assert {"ad_spend", "ad_clicks", "ad_conversions",
                "affiliate_revenue", "affiliate_orders"} <= metrics
        assert res.items[0]["dim"]["channel"] == "affiliate"

    def test_chart_subscription_dispatch(self, env):
        import asyncio

        from insflow.engine.demo import DemoSeeder
        from insflow.engine.subscriptions import SubscriptionService

        async def _run():
            await DemoSeeder("test-ws", days=10).seed()
            svc = SubscriptionService("test-ws")
            sub = await svc.create_metric("会话日报", "ga4_sessions", ["webhook"],
                                          {"webhook_url": "https://example.com/hook"},
                                          days=7)
            return sub, await svc.dispatch_metric_charts()

        sub, out = asyncio.get_event_loop().run_until_complete(_run())
        assert sub["filters"]["kind"] == "metric_chart"
        assert out["sent"] + out["failed"] >= 0      # 无外网时不抛错

    def test_ui_hooks_present(self):
        base = ui_source()
        assert "ifLayoutHistory" in base and "data-cap" in base
        assert "/api/v1/auth/me" in base
        explore = open("insflow/web/templates/explore.html", encoding="utf-8").read()
        assert "exRunSql" in explore and "二维透视" in explore and "exSaveDef" in explore
        insights = open("insflow/web/templates/insights.html", encoding="utf-8").read()
        assert "postComment" in insights
        subs = open("insflow/web/templates/subscriptions.html", encoding="utf-8").read()
        assert "createMetricSub" in subs
