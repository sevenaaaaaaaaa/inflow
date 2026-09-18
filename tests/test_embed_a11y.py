"""测试 P1/P2：图表引擎升级（同环比/注释/预测/Canvas）、嵌入令牌、a11y、即席探索

对齐 docs/10 的 P1（框选缩放、brushing、Canvas 大数据量、即席探索）与 P2（嵌入、a11y）。
"""

import time

import pytest

import insflow.core.files as files_mod
from insflow.core.entities import Workspace
from insflow.core.store import Store, reset_store
from insflow.engine import embed
from insflow.viz import charts as c
from insflow.viz.frame import datapanel
from tests._ui_source import ui_source


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)
    monkeypatch.setenv("INSFLOW_MASTER_KEY", "mk")
    monkeypatch.delenv("INSFLOW_EMBED_SECRET", raising=False)
    s = Store(db_path=tmp_path / "t.db")
    await s.connect()
    await s.migrate()
    reset_store(s)
    await s.create_workspace(Workspace(id="test-ws", name="T"))
    yield {"store": s}
    reset_store(None)
    await s.close()


class TestChartEngineP1:
    def test_compare_series_rendered(self):
        svg = c.line_chart(
            [{"name": "本期", "values": [1, 2, 3]}], ["a", "b", "c"],
            compare={"name": "上期", "series": [{"name": "上期", "values": [1, 1, 2]}]})
        assert "上期" in svg
        assert '"compare"' in svg.replace("&quot;", '"')

    def test_annotations_pin_events(self):
        svg = c.line_chart([{"name": "x", "values": [1, 2, 3]}], ["a", "b", "c"],
                           annotations=[{"pos": 2, "label": "派发动作",
                                         "kind": "action"}])
        assert "派发动作" in svg
        assert '"annotations"' in svg.replace("&quot;", '"')

    def test_forecast_band(self):
        preds, band = c.forecast_series([1, 2, 3, 4, 5, 6], periods=3)
        assert len(preds) == 3 and len(band) == 3
        assert preds[-1] > preds[0]          # 上升趋势外推
        assert c.forecast_series([1, 2], periods=3) == ([], [])  # 样本不足不预测
        svg = c.line_chart([{"name": "x", "values": [1, 2, 3, 4, 5, 6]}],
                           [f"d{i}" for i in range(6)], forecast_periods=3)
        assert "预测" in svg

    def test_canvas_threshold_switch(self):
        n = 600
        pts = [{"name": "x", "values": list(range(n))}]
        labels = [f"d{i}" for i in range(n)]
        svg = c.line_chart(pts, labels, canvas_threshold=400)
        assert "data-chart" in svg and "canvas" in svg.lower()
        small = c.line_chart([{"name": "x", "values": [1, 2]}], ["a", "b"],
                             canvas_threshold=400)
        assert "<svg" in small

    def test_a11y_roles_and_labels(self):
        charts = [
            c.line_chart([{"name": "x", "values": [1, 2]}], ["a", "b"]),
            c.bar_chart([("A", 5), ("B", 3)]),
            c.percent_bar([("正", 3, "var(--ok)"), ("负", 1, "var(--danger)")]),
            c.funnel([("浏览", 100), ("下单", 20)]),
            c.scatter([(1.0, 2.0, "某词")], x_label="搜索量", y_label="排名"),
            c.radar([("内容", 0.5), ("转化", 0.8)]),
            c.gauge(0.6, label="命中率"),
            c.heatmap(["周一"], ["上午", "下午"], [[0.1, 0.2]]),
            c.stacked_bar([("d1", [("正", 3, "var(--ok)"), ("负", 1, "var(--danger)")])]),
        ]
        for svg in charts:
            assert 'role="img"' in svg, svg[:80]
            assert "aria-label" in svg

    def test_table_fallback_a11y(self):
        html = datapanel("趋势", ["时间", "值"], [["d1", 1]], "<svg></svg>")
        assert 'scope="col"' in html
        assert "sr-only" in html and "<caption" in html
        assert 'aria-pressed="true"' in html and 'aria-pressed="false"' in html

    def test_reduced_motion_and_sr_only_in_base(self):
        src = ui_source()
        assert ".sr-only" in src
        assert "prefers-reduced-motion" in src


class TestEmbedToken:
    def test_roundtrip(self, monkeypatch):
        monkeypatch.setenv("INSFLOW_MASTER_KEY", "mk")
        token = embed.mint("w1", "cockpit:traffic", hours=1,
                           branding={"company": "某公司"})
        payload = embed.verify(token)
        assert payload["w"] == "w1" and payload["p"] == "cockpit:traffic"
        assert payload["b"]["company"] == "某公司"

    def test_tamper_rejected(self, monkeypatch):
        monkeypatch.setenv("INSFLOW_MASTER_KEY", "mk")
        token = embed.mint("w1", "metric:gsc_clicks")
        with pytest.raises(embed.EmbedError):
            embed.verify(token[:-3] + "abc")
        with pytest.raises(embed.EmbedError):
            embed.verify("not a token")

    def test_expiry_and_failclosed(self, monkeypatch):
        monkeypatch.setenv("INSFLOW_MASTER_KEY", "mk")
        expired = embed.mint("w1", "metric:x", hours=-1)
        with pytest.raises(embed.EmbedError):
            embed.verify(expired)
        monkeypatch.delenv("INSFLOW_MASTER_KEY", raising=False)
        monkeypatch.delenv("INSFLOW_EMBED_SECRET", raising=False)
        with pytest.raises(embed.EmbedError):
            embed.mint("w1", "metric:x")

    def test_secret_isolated_from_master_when_set(self, monkeypatch):
        monkeypatch.setenv("INSFLOW_MASTER_KEY", "mk")
        monkeypatch.setenv("INSFLOW_EMBED_SECRET", "s1")
        t1 = embed.mint("w1", "metric:x")
        monkeypatch.setenv("INSFLOW_EMBED_SECRET", "s2")
        with pytest.raises(embed.EmbedError):
            embed.verify(t1)

    def test_route_ok_forbidden_and_missing_ws(self, env, monkeypatch):
        from fastapi.testclient import TestClient

        from insflow.server.app import app
        monkeypatch.setenv("INSFLOW_MASTER_KEY", "mk")
        c = TestClient(app)
        assert c.get("/console/embed", params={"token": "bad"}).status_code == 403
        tok = embed.mint("test-ws", "cockpit:traffic")
        r = c.get("/console/embed", params={"token": tok})
        assert r.status_code == 200
        assert "<html" in r.text and "iframe" not in r.text
        missing = embed.mint("nope", "cockpit:traffic")
        assert c.get("/console/embed", params={"token": missing}).status_code == 404

    def test_metric_panel_renders_no_nav(self, env, monkeypatch):
        import asyncio

        from fastapi.testclient import TestClient

        from insflow.server.app import app
        monkeypatch.setenv("INSFLOW_MASTER_KEY", "mk")

        async def _seed():
            for i in range(4):
                await env["store"]._execute(
                    """INSERT INTO metrics (id, workspace_id, entity_type, entity_id,
                       metric, value, dim_json, ts) VALUES (?, 'test-ws', 'topic',
                       '某品牌', 'gsc_clicks', ?, '{}', ?)""",
                    (f"e{i}", 10 + i, f"2026-09-1{i}T00:00:00+00:00"))
            await env["store"]._db.commit()

        asyncio.get_event_loop().run_until_complete(_seed())
        tok = embed.mint("test-ws", "metric:gsc_clicks", branding={"company": "某公司"})
        r = TestClient(app).get("/console/embed", params={"token": tok})
        assert r.status_code == 200
        assert "某公司" in r.text
        assert "gsc_clicks" in r.text
        assert "即席探索" not in r.text  # 只读：无控制台导航
        assert 'role="img"' in r.text and "aria-label" in r.text
        assert 'scope="col"' in r.text   # 数据表回退（屏幕阅读器）

    def test_cockpit_panel_renders_kpis_and_a11y(self, env, monkeypatch):
        from fastapi.testclient import TestClient

        from insflow.server.app import app
        monkeypatch.setenv("INSFLOW_MASTER_KEY", "mk")
        for panel in ("cockpit:overview", "cockpit:sentiment", "cockpit:traffic",
                      "cockpit:competitor", "cockpit:journey",
                      "cockpit:action-loop", "cockpit:reports", "cockpit:ops",
                      "cockpit:billing"):
            tok = embed.mint("test-ws", panel)
            r = TestClient(app).get("/console/embed", params={"token": tok})
            assert r.status_code == 200, panel
            assert "if-main" not in r.text        # 无控制台外壳
            assert "面板不可用" not in r.text, panel
            assert r.text.strip().endswith("</html>")

    def test_iframe_snippet_cache_busters(self, env, monkeypatch):
        """CDN 可能无视 no-store 缓存 HTML：嵌入片段必须自带缓存绕过"""

        from fastapi.testclient import TestClient

        from insflow.server.app import app
        monkeypatch.setenv("INSFLOW_MASTER_KEY", "mk")
        # 直接用令牌端点（非 SAAS 模式下不需要登录）
        r = TestClient(app).get("/console/embed/token",
                                params={"panel": "cockpit:traffic"})
        assert r.status_code == 200
        snippet = r.json()["iframe"]
        assert "_t=" in snippet and "Date.now()" in snippet
        assert "<iframe" in snippet

    def test_cli_token_output(self, monkeypatch):
        from click.testing import CliRunner

        from insflow.cli import main
        monkeypatch.setenv("INSFLOW_MASTER_KEY", "mk")
        res = CliRunner().invoke(main, ["embed", "token", "-w", "w1",
                                        "-p", "metric:gsc_clicks", "--hours", "2"])
        assert res.exit_code == 0, res.output
        assert "iframe" in res.output


class TestExploreAndRange:
    def test_explore_page_with_metric_param(self, env):
        """回归：chart.values 与 dict.values 命名冲突曾致 500"""
        import asyncio

        from fastapi.testclient import TestClient

        from insflow.server.app import app

        async def _seed():
            for i in range(5):
                await env["store"]._execute(
                    """INSERT INTO metrics (id, workspace_id, entity_type, entity_id,
                       metric, value, dim_json, ts) VALUES (?, 'test-ws', 'topic',
                       '某品牌', 'gsc_clicks', ?, '{}', ?)""",
                    (f"x{i}", 5 + i, f"2026-09-1{i}T00:00:00+00:00"))
            await env["store"]._db.commit()

        asyncio.get_event_loop().run_until_complete(_seed())
        c = TestClient(app)
        r = c.get("/console/explore", params={"metric": "gsc_clicks", "days": 7})
        assert r.status_code == 200
        assert "gsc_clicks" in r.text
        assert "y_values" not in r.text  # 内部键名不外泄

    def test_metric_catalog_api(self, env):
        import asyncio

        from fastapi.testclient import TestClient

        from insflow.server.app import app

        async def _seed():
            await env["store"]._execute(
                """INSERT INTO metrics (id, workspace_id, entity_type, entity_id, metric,
                   value, dim_json, ts) VALUES ('c1','test-ws','topic','某品牌',
                   'topic_negative_ratio', 0.4, '{}', '2026-09-10T00:00:00+00:00')""")
            await env["store"]._db.commit()

        asyncio.get_event_loop().run_until_complete(_seed())
        r = TestClient(app).get("/api/v1/metrics/catalog",
                                params={"workspace_id": "test-ws"})
        assert r.status_code == 200
        names = [m["metric"] for m in r.json()["metrics"]]
        assert "topic_negative_ratio" in names

    def test_range_api_returns_insights_and_actions(self, env):
        import asyncio

        from fastapi.testclient import TestClient

        from insflow.core.entities import Action, Insight
        from insflow.server.app import app

        async def _seed():
            ins = await env["store"].create_insight(Insight(
                workspace_id="test-ws", type="topic_negative_alert",
                title="负面预警", summary="占比 45%", severity="high"))
            await env["store"].create_action(Action(
                workspace_id="test-ws", insight_id=ins.id, action_type="send_report",
                state="verified", result_json={"verdict": "有效"}))
            await env["store"]._db.commit()

        asyncio.get_event_loop().run_until_complete(_seed())
        today = time.strftime("%Y-%m-%d")
        r = TestClient(app).get("/api/v1/charts/range", params={
            "workspace_id": "test-ws", "from": "2020-01-01", "to": today})
        assert r.status_code == 200
        data = r.json()
        assert any(i["title"] == "负面预警" for i in data["insights"])
        assert any(a["verdict"] == "有效" for a in data["actions"])
