"""Batch6 测试：实时流（SSE）/ 在线协同 / 拖拽透视 / 表达式级 RLS / 稳健异常 / OIDC SSO"""

import os

import pytest

import insflow.core.files as files_mod
from insflow.core.entities import Workspace
from insflow.core.store import Store, reset_store
from insflow.engine import sso
from insflow.engine.realtime import Presence, StreamHub, parse_metrics, sse_event
from insflow.engine.rls import (DENY_ALL, PolicyError, build_policy, compile_policy,
                                policies_for)
from insflow.viz import charts as c


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


class TestRealtimeStream:
    async def _seed(self, env, days: int = 10):
        from insflow.engine.demo import DemoSeeder
        await DemoSeeder("test-ws", days=days).seed()

    def test_sse_frame_format(self):
        frame = sse_event("metrics", {"a": 1}, event_id="e1")
        assert frame.startswith("id: e1\nevent: metrics\ndata: ")
        assert frame.endswith("\n\n")
        assert sse_event("heartbeat", {"b": 2}).startswith("event: heartbeat")

    def test_metric_name_whitelist(self):
        assert parse_metrics("ga4_sessions,gsc_clicks") == ["ga4_sessions", "gsc_clicks"]
        assert parse_metrics("ga4_sessions; DROP TABLE") == []      # 整串被拒（更保守）
        assert parse_metrics("ga4_sessions,gsc_clicks") == ["ga4_sessions", "gsc_clicks"]
        assert parse_metrics("") == []

    def test_hub_emits_metrics_then_heartbeat(self, env):
        import asyncio

        async def _run():
            await self._seed(env)
            hub = StreamHub("test-ws", metrics=["ga4_sessions"], days=10,
                            interval=0.1, max_ticks=3, heartbeat=0.1)
            out = []
            async for ev, data in hub.events():
                out.append(ev)
            return out

        events = asyncio.get_event_loop().run_until_complete(_run())
        assert events[0] == "metrics" and events[-1] == "done"

    def test_presence_lifecycle_and_ttl(self, monkeypatch):
        p = Presence()
        p.touch("w", "c1", user="seven", path="/x", cursor={"x": 0.5, "y": 0.5})
        p.touch("w", "c2", user="alice")
        snap = p.snapshot("w")
        assert {e["user"] for e in snap} == {"seven", "alice"}
        assert [e["cursor"] for e in snap if e["conn_id"] == "c1"][0] == {"x": 0.5, "y": 0.5}
        p.leave("w", "c2")
        assert [e["conn_id"] for e in p.snapshot("w")] == ["c1"]
        # 超时（TTL）自动清理
        for e in p._by_ws["w"].values():
            e["last_seen"] = 0
        assert p.snapshot("w") == []

    def test_presence_cap(self):
        p = Presence()
        for i in range(80):
            p.touch("w", f"c{i}")
        assert len(p.snapshot("w")) <= 50

    def test_stream_and_presence_http(self, env):
        import asyncio
        from fastapi.testclient import TestClient
        from insflow.server.app import app

        asyncio.get_event_loop().run_until_complete(self._seed(env))
        cli = TestClient(app)
        r = cli.get("/api/v1/stream", params={
            "workspace_id": "test-ws", "metrics": "ga4_sessions",
            "interval": 2, "max_ticks": 2})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers.get("x-accel-buffering") == "no"      # 反代不缓冲
        assert "event: metrics" in r.text and "event: done" in r.text
        assert "ga4_sessions" in r.text
        ok = cli.post("/api/v1/presence", json={
            "workspace_id": "test-ws", "conn_id": "c1",
            "cursor": {"x": 0.1, "y": 0.2}}).json()
        assert ok["ok"] and ok["online"] == 1
        assert cli.get("/api/v1/presence",
                       params={"workspace_id": "test-ws"}).json()["online"]
        cli.post("/api/v1/presence/leave", json={"workspace_id": "test-ws",
                                                "conn_id": "c1"})
        assert cli.get("/api/v1/presence",
                       params={"workspace_id": "test-ws"}).json()["online"] == []
        assert cli.post("/api/v1/presence", json={"workspace_id": ""}).status_code == 400

    def test_page_has_live_and_presence_hooks(self):
        base = open("insflow/web/templates/base.html", encoding="utf-8").read()
        for token in ("ifStartLive", "EventSource", "ifPaintLive", "ifPresenceTick",
                      "if-cursor", "if-online", "sendBeacon"):
            assert token in base, token
        assert "data-live-metrics" in base


class TestDragDropCube:
    async def _seed(self, env, days: int = 30):
        from insflow.engine.demo import DemoSeeder
        await DemoSeeder("test-ws", days=days).seed()

    def test_cube_1d_2d_and_no_dim(self, env):
        import asyncio

        async def _run():
            await self._seed(env)
            store = env["store"]
            two = await store.cube("test-ws", "ga4_sessions", ["province"], "channel",
                                   days=30)
            one = await store.cube("test-ws", "ga4_sessions", ["province"], days=30)
            none = await store.cube("test-ws", "ga4_sessions", [], days=30)
            return two, one, none

        two, one, none = asyncio.get_event_loop().run_until_complete(_run())
        assert two["row_labels"] and two["col_labels"] and two["dim_col"] == "channel"
        assert one["dim_col"] == "" and len(one["col_labels"]) > 3   # 时间列
        assert none["col_labels"] == ["ga4_sessions"] and len(none["matrix"]) > 5

    def test_cube_rejects_unknown_dims(self, env):
        import asyncio

        async def _run():
            store = env["store"]
            return await store.cube("test-ws", "ga4_sessions",
                                    ["evil_dim", "province"], "also_evil", days=7)

        res = asyncio.get_event_loop().run_until_complete(_run())
        assert res["dim_rows"] == ["province"] and res["dim_col"] == ""

    def test_dims_and_cube_api(self, env):
        import asyncio
        from fastapi.testclient import TestClient
        from insflow.server.app import app

        asyncio.get_event_loop().run_until_complete(self._seed(env))
        cli = TestClient(app)
        dims = cli.get("/api/v1/dims", params={"workspace_id": "test-ws"}).json()["dims"]
        assert "province" in dims and "channel" in dims
        r = cli.post("/api/v1/explore/cube", json={
            "workspace_id": "test-ws", "metric": "ga4_sessions",
            "rows": ["province"], "cols": "channel", "days": 30})
        assert r.status_code == 200 and r.json()["row_labels"]
        assert cli.post("/api/v1/explore/cube", json={
            "workspace_id": "test-ws", "metric": ""}).status_code == 400

    def test_explore_page_has_drag_zones(self):
        page = open("insflow/web/templates/explore.html", encoding="utf-8").read()
        for token in ("exDragDim", "exDrop", "exRunCube", "slot-rows", "slot-cols",
                      "slot-val", "exClearCube"):
            assert token in page, token


class TestRlsPolicies:
    def test_compile_basic(self):
        sql, params = compile_policy("channel = 'search' and province in ('广东','北京')")
        assert "json_extract(dim_json, '$.channel') = ?" in sql
        assert "IN (?, ?)" in sql and params == ["search", "广东", "北京"]

    def test_compile_user_placeholders(self):
        sql, params = compile_policy("entity_id = '{user.email}'",
                                    {"email": "a@b.com"})
        assert sql == "entity_id = ?" and params == ["a@b.com"]

    def test_numeric_and_comparison(self):
        sql, params = compile_policy("value > 100")
        assert sql == "value > ?" and params == [100.0]

    def test_like_guard(self):
        sql, _ = compile_policy("channel like 'search%'")
        assert "LIKE ?" in sql
        with pytest.raises(PolicyError):
            compile_policy("channel like '%search'")      # 禁止通配开头

    def test_rejects_non_whitelisted(self):
        for bad in ["user_id = 1", "channel == 'x'", "channel", "1 = 1",
                    "channel = 'x'; drop table metrics", "select * from metrics"]:
            with pytest.raises(PolicyError):
                compile_policy(bad)

    def test_build_union_and_failclosed(self):
        settings = {"rls_policies": {"viewer": ["channel = 'a'", "device = 'mobile'"]}}
        sql, params = build_policy(settings, "viewer")
        assert "OR" in sql and params == ["a", "mobile"]
        assert build_policy(settings, "analyst") == ("", [])          # 该角色无策略
        broken = {"rls_policies": {"viewer": ["channel === broken"]}}
        assert build_policy(broken, "viewer") == DENY_ALL             # 解析失败 → 全拒
        assert policies_for(settings, "viewer") == ["channel = 'a'",
                                                   "device = 'mobile'"]

    def test_store_enforces_policy(self, env):
        import asyncio

        async def _run():
            store = env["store"]
            rows = [("m1", "search", 10), ("m2", "social", 20)]
            for mid, ch, v in rows:
                await store._execute(
                    """INSERT INTO metrics (id, workspace_id, entity_type, entity_id,
                       metric, value, dim_json, ts) VALUES (?, 'test-ws', 'site', 'A',
                       'ga4_sessions', ?, ?, '2026-09-10T00:00:00+00:00')""",
                    (mid, v, '{"channel": "%s"}' % ch))
            await store._db.commit()
            everything = await store.metric_total("test-ws", "ga4_sessions", days=3650)
            only_search = await store.metric_total(
                "test-ws", "ga4_sessions", days=3650,
                policy=build_policy({"rls_policies": {"viewer": ["channel = 'search'"]}},
                                    "viewer"))
            denied = await store.metric_total(
                "test-ws", "ga4_sessions", days=3650,
                policy=build_policy({"rls_policies": {"viewer": ["bad =="]}}, "viewer"))
            social_total = await store.metric_total(
                "test-ws", "ga4_sessions", days=3650,
                policy=build_policy({"rls_policies": {"viewer": ["channel = 'social'"]}},
                                    "viewer"))
            # 长窗口 + policy 时必须走明细（预聚合快路径不得绕过权限）
            series = await store.metric_series(
                "test-ws", "ga4_sessions", days=3650,
                policy=build_policy({"rls_policies": {"viewer": ["channel = 'social'"]}},
                                    "viewer"))
            return everything, only_search, denied, series, social_total

        everything, only_search, denied, series, social_total = \
            asyncio.get_event_loop().run_until_complete(_run())
        assert everything == 30 and only_search == 10 and denied == 0
        assert social_total == 20
        assert series and series[0]["value"] == 20      # 未被快路径绕过


class TestRobustAnomaly:
    def test_mad_beats_sigma_on_spike(self):
        vals = [10, 11, 10, 12, 11, 10, 11, 12, 11, 10, 60]
        assert 3 in c.anomaly_points(vals)[2]           # σ 口径误报
        assert c.anomaly_points_robust(vals)[2] == [10]  # MAD 只报真异常

    def test_flat_series_no_false_positive(self):
        assert c.anomaly_points_robust([5] * 12)[2] == []
        assert c.anomaly_points_robust([1, 2, 3])[2] == []

    def test_seasonal_residual_detects_break(self):
        vals = [10, 20, 30] * 4 + [999]
        assert c.anomaly_points_seasonal(vals)[2] == [12]
        # 样本不足时回落稳健法
        assert c.anomaly_points_seasonal([1, 2, 3])[2] == []

    def test_chart_records_method(self):
        import html
        svg = c.line_chart([{"name": "x", "values": [1] * 8 + [50]}],
                           [f"d{i}" for i in range(9)],
                           anomaly=True, anomaly_method="mad")
        assert '"anomaly_method": "mad"' in html.unescape(svg)
        assert "异常标记（mad）" in svg


class TestSsoOidc:
    def test_disabled_without_config(self, monkeypatch):
        for key in ("INSFLOW_OIDC_ISSUER", "INSFLOW_OIDC_CLIENT_ID"):
            monkeypatch.delenv(key, raising=False)
        assert not sso.enabled()
        with pytest.raises(sso.SsoError):
            import asyncio
            asyncio.run(sso.authorize_url("https://app/cb", "st"))

    def _fake_idp(self, monkeypatch, *, basic=False, email="user@example.com",
                  verified=True):
        monkeypatch.setenv("INSFLOW_OIDC_ISSUER", "https://idp.example.com")
        monkeypatch.setenv("INSFLOW_OIDC_CLIENT_ID", "cid")
        monkeypatch.setenv("INSFLOW_OIDC_CLIENT_SECRET", "sec")
        calls = {}

        async def fake(method, url, **kw):
            calls.setdefault("urls", []).append(url)
            if "openid-configuration" in url:
                return {"authorization_endpoint": "https://idp.example.com/auth",
                        "token_endpoint": "https://idp.example.com/token",
                        "userinfo_endpoint": "https://idp.example.com/userinfo",
                        "token_endpoint_auth_methods_supported":
                            ["client_secret_basic"] if basic else ["client_secret_post"]}
            if url.endswith("/token"):
                calls["token_kw"] = kw
                return {"access_token": "at-1"}
            if url.endswith("/userinfo"):
                payload = {"name": "SSO", "sub": "u1"}
                if email:
                    payload["email"] = email
                if verified is False:
                    payload["email_verified"] = False
                return payload
            raise AssertionError(url)

        monkeypatch.setattr(sso, "_http_json", fake)
        sso._discovery_cache.clear()
        return calls

    def test_full_flow_post_secret(self, monkeypatch):
        import asyncio
        calls = self._fake_idp(monkeypatch)
        url = asyncio.run(sso.authorize_url("https://app/cb", "st-1"))
        assert url.startswith("https://idp.example.com/auth?")
        assert "state=st-1" in url
        tokens = asyncio.run(sso.exchange_code("code1", "https://app/cb"))
        assert tokens["access_token"] == "at-1"
        assert calls["token_kw"]["data"]["client_secret"] == "sec"
        info = asyncio.run(sso.fetch_userinfo("at-1"))
        assert info["email"] == "user@example.com" and info["role"] == "analyst"

    def test_basic_auth_and_rejections(self, monkeypatch):
        import asyncio
        calls = self._fake_idp(monkeypatch, basic=True)
        asyncio.run(sso.exchange_code("c", "https://app/cb"))
        assert calls["token_kw"]["auth"] == ("cid", "sec")

        self._fake_idp(monkeypatch, email="")
        with pytest.raises(sso.SsoError):
            asyncio.run(sso.fetch_userinfo("at"))
        self._fake_idp(monkeypatch, verified=False)
        with pytest.raises(sso.SsoError):
            asyncio.run(sso.fetch_userinfo("at"))

    def test_upsert_user_provision_and_reuse(self, env, monkeypatch):
        import asyncio
        self._fake_idp(monkeypatch)

        async def _run():
            first = await sso.upsert_user("test-ws", {"email": "new@example.com",
                                                     "name": "New", "role": "analyst"})
            again = await sso.upsert_user("test-ws", {"email": "new@example.com",
                                                     "name": "New", "role": "analyst"})
            return first, again

        first, again = asyncio.run(_run())
        assert first["created"] and first["role"] == "analyst"
        assert not again["created"] and again["user_id"] == first["user_id"]

    def test_routes_flow_with_state_check(self, env, monkeypatch):
        import asyncio
        from fastapi.testclient import TestClient
        from insflow.server.app import app
        monkeypatch.setenv("INSFLOW_SAAS", "1")
        calls = self._fake_idp(monkeypatch)

        async def _seed():
            from insflow.core.accounts import AccountManager
            await AccountManager("test-ws").register("owner@test.com", "password123",
                                                     "Owner", "T")
        asyncio.run(_seed())
        cli = TestClient(app)
        assert "SSO 单点登录" in cli.get("/console/login").text
        r = cli.get("/console/sso/login", params={"next": "/console"},
                    follow_redirects=False)
        assert r.status_code == 303 and "idp.example.com" in r.headers["location"]
        assert "httponly" in r.headers["set-cookie"].lower()
        state = cli.cookies.get(sso.state_cookie_name())
        bad = cli.get("/console/sso/callback", params={"code": "c", "state": "nope"},
                      follow_redirects=False)
        assert bad.status_code == 400
        ok = cli.get("/console/sso/callback", params={"code": "c", "state": state},
                     follow_redirects=False)
        assert ok.status_code == 303
        assert cli.get("/console", follow_redirects=False).status_code == 200
        assert calls["urls"], "应访问 IdP discovery/token/userinfo"

    def test_route_without_config_is_explicit(self, env, monkeypatch):
        from fastapi.testclient import TestClient
        from insflow.server.app import app
        for key in ("INSFLOW_OIDC_ISSUER", "INSFLOW_OIDC_CLIENT_ID"):
            monkeypatch.delenv(key, raising=False)
        r = TestClient(app).get("/console/sso/login")
        assert r.status_code == 400 and "未启用 SSO" in r.text


class TestRlsEndToEnd:
    """策略 → 看板数据收敛（端到端，含预聚合快路径不得绕过）"""

    def _seed(self, env):
        import asyncio
        from insflow.engine.demo import DemoSeeder
        asyncio.get_event_loop().run_until_complete(
            DemoSeeder("test-ws", days=30).seed())

    def test_rls_api_validates_and_persists(self, env):
        from fastapi.testclient import TestClient
        from insflow.server.app import app
        self._seed(env)
        cli = TestClient(app)
        r = cli.post("/api/v1/rls/policies", json={
            "workspace_id": "test-ws",
            "policies": {"viewer": ["channel = 'search'"],
                         "analyst": ["province in ('广东','北京')"]}})
        assert r.status_code == 200
        assert cli.get("/api/v1/rls/policies",
                       params={"workspace_id": "test-ws"}).json()["policies"]["viewer"]
        bad = cli.post("/api/v1/rls/policies", json={
            "workspace_id": "test-ws", "policies": {"viewer": ["channel === bad"]}})
        assert bad.status_code == 400                      # 坏策略不入库
        assert cli.post("/api/v1/rls/policies", json={
            "workspace_id": "test-ws", "policies": {"superuser": ["x = 1"]}}
        ).status_code == 400

    def _kpi_numbers(self, html: str) -> set:
        import re
        return set(re.findall(r'class="num"[^>]*>\s*([\d,]+)', html))

    def test_policy_scopes_traffic_cockpit(self, env):
        import asyncio
        from fastapi.testclient import TestClient
        from insflow.server.app import app
        self._seed(env)
        cli = TestClient(app)
        before = cli.get("/console/cockpit/traffic", params={
            "workspace_id": "test-ws", "days": 30}).text
        nums_before = self._kpi_numbers(before)

        async def _set():
            store = env["store"]
            ws = await store.get_workspace("test-ws")
            settings = dict(ws.settings_json or {})
            settings["rls_policies"] = {"owner": ["channel = 'search'"]}
            ws.settings_json = settings
            await store.update_workspace(ws)

        asyncio.get_event_loop().run_until_complete(_set())
        r = cli.get("/console/cockpit/traffic", params={"workspace_id": "test-ws",
                                                       "days": 30})
        assert r.status_code == 200
        nums_after = self._kpi_numbers(r.text)
        # 策略生效：可见数据的 KPI 数字集合发生变化（渠道被收敛）
        assert nums_after and nums_after != nums_before
