"""端到端冒烟：走真实 HTTP 路由（TestClient），覆盖关键用户路径

定位：把「改了 A 崩了 B」挡在 CI，而不是等到线上手测。
运行：pytest tests/e2e -q（或 make e2e）
"""
import pytest

pytestmark = pytest.mark.e2e

WS = "e2e-ws"
COCKPITS = ("overview", "sentiment", "traffic", "competitor", "journey",
            "action-loop", "reports", "ops", "billing")


class TestConsolePaths:
    def test_dashboard_and_cockpits(self, client):
        assert client.get("/console", params={"workspace_id": WS}).status_code == 200
        for name in COCKPITS:
            r = client.get(f"/console/cockpit/{name}",
                           params={"workspace_id": WS, "days": 20})
            assert r.status_code == 200, name
            assert "Insight Flow" in r.text
            # 每舱必须至少有一块内容（图或表），避免"空壳页"回归
            assert ('class="dp"' in r.text or "card" in r.text), name
        assert client.get("/console/sentiment",
                          params={"workspace_id": WS}).status_code == 200

    def test_explore_variants(self, client):
        for chart in ("line", "bar", "box", "scatter", "map", "treemap",
                      "waterfall", "calendar", "candlestick"):
            r = client.get("/console/explore", params={
                "workspace_id": WS, "metric": "ga4_sessions", "days": 20,
                "chart": chart})
            assert r.status_code == 200, chart
            assert "<svg" in r.text or "data-canvas" in r.text, chart
        # 派生指标（语义层）+ 表计算
        client.post("/api/v1/metrics/defs", json={
            "workspace_id": WS, "name": "e2e_cvr",
            "expr": "ga4_conversions / ga4_sessions * 100", "unit": "%"})
        r = client.get("/console/explore", params={
            "workspace_id": WS, "metric": "e2e_cvr", "days": 20})
        assert r.status_code == 200 and "派生指标" in r.text
        r2 = client.get("/console/explore", params={
            "workspace_id": WS, "metric": "ga4_sessions", "days": 20, "calc": "cum"})
        assert r2.status_code == 200

    def test_mobile_audit_and_pwa(self, client):
        assert client.get("/console/m", params={"workspace_id": WS}).status_code == 200
        assert client.get("/console/audit", params={"workspace_id": WS}).status_code == 200
        assert client.get("/console/manifest.webmanifest").status_code == 200
        assert client.get("/console/sw.js").status_code == 200
        assert client.get("/console/icon.svg").status_code == 200

    def test_a11y_and_interaction_hooks(self, client):
        html = client.get("/console/cockpit/traffic",
                          params={"workspace_id": WS, "days": 20}).text
        # 页面内联部分：语义与无障碍标记
        for hook in ('role="img"', "aria-label", 'scope="col"', "sr-only",
                     "data-annot"):
            assert hook in html, hook
        # 交互脚本已抽到可缓存静态资源：从这里断言（并确认页面确实外链了它）
        assert "/console/static/app.js?v=" in html
        js = client.get("/console/static/app.js").text
        for hook in ("ifToggleLive", "ifPresenceTick", "ifCrossFilter",
                     "ifToggleSeries", "ifAskRun"):
            assert hook in js, hook
        css = client.get("/console/static/app.css").text
        assert "prefers-reduced-motion" in css and ".sr-only" in css


class TestApiPaths:
    def test_exports(self, client):
        x = client.get("/api/v1/export/xlsx", params={
            "workspace_id": WS, "panel": "cockpit:traffic", "days": 20})
        assert x.status_code == 200 and x.content[:2] == b"PK"
        assert x.headers.get("x-export-watermark")
        csv = client.get("/api/v1/audit/admin", params={
            "workspace_id": WS, "format": "csv"})
        assert csv.status_code == 200 and csv.text.startswith("# 导出水印")

    def test_embed_roundtrip(self, client):
        t = client.get("/console/embed/token", params={"panel": "board:traffic"})
        assert t.status_code == 200
        token = t.json()["token"]
        view = client.get("/console/embed", params={"token": token})
        assert view.status_code == 200 and 'class="dp"' in view.text
        assert "即席探索" not in view.text                 # 只读：无控制台导航
        assert client.get("/console/embed",
                          params={"token": "bad"}).status_code == 403

    def test_dataset_and_analytics_apis(self, client):
        assert client.get("/api/v1/metrics/catalog",
                          params={"workspace_id": WS}).status_code == 200
        assert client.get("/api/v1/dims", params={"workspace_id": WS}).status_code == 200
        assert client.post("/api/v1/explore/cube", json={
            "workspace_id": WS, "metric": "ga4_sessions", "rows": ["province"],
            "cols": "channel", "days": 20}).status_code == 200
        assert client.post("/api/v1/explore/sql", json={
            "workspace_id": WS,
            "sql": "SELECT metric FROM metrics GROUP BY metric"}).status_code == 200
        assert client.post("/api/v1/explore/sql", json={
            "workspace_id": WS, "sql": "DROP TABLE metrics"}).status_code == 400

    def test_insight_loop_and_verification(self, client):
        # 叙事 → 沉淀洞察 → 派发动作 → 验证统计
        pub = client.post("/api/v1/narrative/insight", json={
            "workspace_id": WS, "metric": "ga4_sessions", "days": 20})
        assert pub.status_code == 200
        iid = pub.json()["insight_id"]
        ins = client.get(f"/api/v1/insights/{iid}")
        assert ins.status_code == 200
        assert client.get("/api/v1/attribution/lift",
                          params={"workspace_id": WS}).status_code == 200
        assert client.get("/api/v1/attribution/channels",
                          params={"workspace_id": WS}).status_code == 200
        assert client.get("/api/v1/data-quality",
                          params={"workspace_id": WS}).status_code == 200

    def test_annotations_and_audit(self, client):
        assert client.post("/api/v1/comments", json={
            "workspace_id": WS, "target_type": "chart", "target_id": "traffic-trend",
            "body": "E2E 批注"}).status_code == 200
        counts = client.get("/api/v1/comments/counts", params={
            "workspace_id": WS, "target_type": "chart"}).json()["counts"]
        assert counts.get("traffic-trend") == 1
        logs = client.get("/api/v1/audit/admin",
                          params={"workspace_id": WS}).json()["logs"]
        assert logs

    def test_privacy_and_scim_guard(self, client):
        assert client.get("/api/v1/privacy/subject/export", params={
            "workspace_id": WS, "email": "nobody@example.com"}).status_code == 200
        assert client.post("/api/v1/privacy/retention/sweep", json={
            "workspace_id": WS, "dry_run": True}).status_code == 200
        # SCIM 未配置 token → fail-closed
        assert client.get("/scim/v2/Users",
                          params={"workspace_id": WS}).status_code == 401

    def test_health_and_observability(self, client):
        assert client.get("/health").status_code == 200
        r = client.get("/api/v1/observability", params={"workspace_id": WS})
        assert r.status_code == 200
        body = r.json()
        for key in ("perf", "cache", "db", "data_quality"):
            assert key in body, key


class TestUiHarness:
    """把 Node UI 行为断言纳入常规测试（无 node 时跳过）"""

    def test_ui_behaviour_node(self, client, tmp_path):
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            pytest.skip("未安装 node")
        page = client.get("/console/m").text
        assert "/console/static/app.js" in page          # 交互脚本已外链
        js = tmp_path / "app.js"
        js.write_text(client.get("/console/static/app.js").text, encoding="utf-8")
        proc = subprocess.run([node, "scripts/e2e_ui.mjs", str(js)],
                              capture_output=True, text=True, timeout=90)
        assert proc.returncode == 0, proc.stdout[-800:] + proc.stderr[-400:]
        assert "5/5" in proc.stdout
