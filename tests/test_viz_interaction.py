"""测试 P0 交互：图表面板（表/CSV）、下钻 API、筛选栏（对齐 docs/10 P0）"""

import csv
import io
from urllib.parse import unquote

import pytest

import insflow.core.files as files_mod
from insflow.core.entities import Insight, Workspace
from insflow.core.store import Store, reset_store
from insflow.viz import charts as c
from insflow.viz.frame import datapanel


class TestDatapanel:
    def test_toolbar_table_csv(self):
        html = datapanel("测试图", ["主体", "值"], [["A", 1], ["B", 2]],
                         c.bar_chart([("A", 1), ("B", 2)]), csv_name="t")
        assert 'class="dp"' in html
        assert html.count("dp-btn") >= 3                    # 图 / 表 / CSV
        assert "<table" in html and "dp-tablewrap" in html   # 服务端渲染数据表
        assert 'download="t.csv"' in html

    def test_csv_payload_correct(self):
        html = datapanel("t", ["k", "v"], [["a", "1"], ["b", "2"]], "<svg></svg>", csv_name="x")
        start = html.index("data:text/csv;charset=utf-8,") + len("data:text/csv;charset=utf-8,")
        end = html.index('"', start)
        content = unquote(html[start:end])
        rows = list(csv.reader(io.StringIO(content)))
        assert rows[0] == ["k", "v"] and rows[1] == ["a", "1"] and rows[2] == ["b", "2"]

    def test_drill_attributes(self):
        html = datapanel("t", ["a"], [["x", 1]], "<svg></svg>", csv_name="x",
                         drill_metric="m1", drill_entity="e1")
        assert 'data-drill-metric="m1"' in html and 'data-drill-entity="e1"' in html

    def test_escapes_title(self):
        html = datapanel("<script>x</script>", ["a"], [["b", 1]], "<svg></svg>")
        assert "<script>x" not in html


class TestChartTooltips:
    def test_line_has_tooltip_and_crosshair_data(self):
        s = c.line_chart([{"name": "a", "values": [1, 2, 3]}], ["d1", "d2", "d3"])
        assert "data-chart" in s and s.count("data-tip") == 3

    def test_bar_tooltip_and_drill(self):
        s = c.bar_chart([("主体A", 5)])
        assert "data-tip" in s and "data-drill-entity" in s

    def test_percent_and_funnel_and_heatmap_tips(self):
        assert "data-tip" in c.percent_bar([("正", 1, "var(--ok)")])
        assert "data-tip" in c.funnel([("A", 10), ("B", 3)])
        assert "data-tip" in c.heatmap(["r"], ["c"], [[0.5]])

    def test_scatter_tooltip_and_drill(self):
        s = c.scatter([(1, 2, "kw")], x_label="x", y_label="y")
        assert "data-tip" in s and "data-drill-entity" in s


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)
    monkeypatch.setenv("INSFLOW_MASTER_KEY", "mk")
    s = Store(db_path=tmp_path / "t.db")
    await s.connect()
    await s.migrate()
    reset_store(s)
    await s.create_workspace(Workspace(id="test-ws", name="T"))
    yield {"store": s}
    reset_store(None)
    await s.close()


class TestDrillAPI:
    def test_endpoint_returns_series_and_insights(self, env):
        import asyncio

        from fastapi.testclient import TestClient

        from insflow.server.app import app

        async def _seed():
            store = env["store"]
            for i in range(5):
                await store._execute(
                    """INSERT INTO metrics (id, workspace_id, entity_type, entity_id, metric,
                       value, dim_json, ts) VALUES (?, 'test-ws', 'topic', '某品牌',
                       'topic_negative_ratio', ?, '{}', ?)""",
                    (f"m{i}", 0.2 + i * 0.05, f"2026-09-1{i}T00:00:00+00:00"))
            await store.create_insight(Insight(
                workspace_id="test-ws", type="topic_negative_alert",
                title="舆情负面预警：某品牌", summary="负向占比 45%",
                evidence_json=[{"query": "某品牌"}], severity="high"))
            await store._db.commit()

        asyncio.get_event_loop().run_until_complete(_seed())
        c = TestClient(app)
        r = c.get("/api/v1/charts/drill", params={
            "workspace_id": "test-ws", "metric": "topic_negative_ratio",
            "entity_id": "某品牌"})
        assert r.status_code == 200
        data = r.json()
        assert len(data["rows"]) == 5
        assert any("某品牌" in i["title"] for i in data["insights"])

    def test_endpoint_empty_ok(self, env):
        from fastapi.testclient import TestClient

        from insflow.server.app import app
        c = TestClient(app)
        r = c.get("/api/v1/charts/drill", params={
            "workspace_id": "test-ws", "metric": "nonexistent", "entity_id": "x"})
        assert r.status_code == 200 and r.json()["rows"] == []


class TestFilterBar:
    def test_filter_bar_renders_and_links(self, env):
        import asyncio

        from fastapi.testclient import TestClient

        from insflow.server.app import app

        async def _seed():
            store = env["store"]
            await store._execute(
                """INSERT INTO metrics (id, workspace_id, entity_type, entity_id, metric,
                   value, dim_json, ts) VALUES ('t1','test-ws','topic','某品牌',
                   'topic_negative_ratio', 0.4, '{}', '2026-09-10T00:00:00+00:00')""")
            await store._db.commit()

        asyncio.get_event_loop().run_until_complete(_seed())
        c = TestClient(app)
        r = c.get("/console/sentiment")
        assert r.status_code == 200
        assert 'class="if-filter"' in r.text
        assert "ifSetFilter('days','30')" in r.text      # 时间范围联动
        assert "ifSetFilter('entity'" in r.text          # 主体筛选（有主体时出现）
        assert "data-ws=" in r.text                      # 下钻需要

    def test_filter_affects_query(self, env):
        """筛选参数进入 URL（可分享）并由路由透传（days/entity）"""
        from fastapi.testclient import TestClient

        from insflow.server.app import app
        c = TestClient(app)
        r = c.get("/console/sentiment", params={"days": 90, "entity": "某品牌"})
        assert r.status_code == 200
