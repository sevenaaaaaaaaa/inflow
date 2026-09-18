"""Batch11 测试：图表级批注协作 + 数据主体请求/留存策略"""

import json
from datetime import UTC, datetime, timedelta

import pytest

import insflow.core.files as files_mod
from insflow.core.entities import Workspace
from insflow.core.store import Store, reset_store
from insflow.viz.frame import datapanel


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


class TestChartAnnotations:
    def test_datapanel_has_stable_panel_key(self):
        html = datapanel("会话趋势", ["a"], [["x", 1]], "<svg></svg>",
                         csv_name="traffic-trend")
        assert 'data-panel="traffic-trend"' in html
        assert 'id="dptraffic-trend"' in html
        assert 'data-annot="traffic-trend"' in html
        assert "ifPanelComments" in html
        # 同一 key 两次渲染 id 相同（批注锚点稳定，不随行数变化）
        again = datapanel("会话趋势", ["a"], [["x", 1], ["y", 2]], "<svg></svg>",
                          csv_name="traffic-trend")
        assert 'data-panel="traffic-trend"' in again

    def test_annot_api_and_counts(self, env):
        import asyncio
        from fastapi.testclient import TestClient
        from insflow.server.app import app

        cli = TestClient(app)
        assert cli.post("/api/v1/comments", json={
            "workspace_id": "test-ws", "target_type": "chart",
            "target_id": "traffic-trend", "body": "这段异常要复核"}).status_code == 200
        assert cli.post("/api/v1/comments", json={
            "workspace_id": "test-ws", "target_type": "chart",
            "target_id": "traffic-trend", "body": "第二条"}).status_code == 200
        counts = cli.get("/api/v1/comments/counts",
                         params={"workspace_id": "test-ws",
                                 "target_type": "chart"}).json()["counts"]
        assert counts == {"traffic-trend": 2}
        listed = cli.get("/api/v1/comments", params={
            "workspace_id": "test-ws", "target_type": "chart",
            "target_id": "traffic-trend"}).json()["comments"]
        assert len(listed) == 2 and listed[0]["body"]

    def test_annotation_does_not_create_action(self, env):
        """批注是讨论，不得触发动作（动作必须走派发链路）"""
        import asyncio
        from fastapi.testclient import TestClient
        from insflow.server.app import app

        async def _run():
            store = env["store"]
            return len(await store.list_actions("test-ws"))
        before = asyncio.get_event_loop().run_until_complete(_run())
        cli = TestClient(app)
        cli.post("/api/v1/comments", json={"workspace_id": "test-ws",
                                          "target_type": "chart",
                                          "target_id": "p1", "body": "x"})
        after = asyncio.get_event_loop().run_until_complete(_run())
        assert before == after == 0

    def test_page_badges_hook(self, env):
        import asyncio
        from fastapi.testclient import TestClient
        from insflow.engine.demo import DemoSeeder
        from insflow.server.app import app

        asyncio.get_event_loop().run_until_complete(
            DemoSeeder("test-ws", days=7).seed())
        page = TestClient(app).get("/console/cockpit/traffic",
                                   params={"workspace_id": "test-ws"})
        assert page.status_code == 200
        assert "ifLoadAnnotCounts" in page.text
        assert 'data-annot=' in page.text


class TestPrivacySubjectAndRetention:
    def test_export_subject(self, env):
        import asyncio
        from insflow.engine.privacy import export_subject

        async def _run():
            store = env["store"]
            from insflow.core.accounts import AccountManager
            await AccountManager("test-ws").register("subj@test.com", "password123",
                                                     "Subj", "T")
            await store.save_journey_event("test-ws", identity="subj@test.com",
                                           stage="visit", event="touch",
                                           props={"channel": "search"})
            await store.add_comment("test-ws", "chart", "traffic-trend", "看这里",
                                    author="subj@test.com")
            return await export_subject("test-ws", "subj@test.com")

        data = asyncio.get_event_loop().run_until_complete(_run())
        assert data["subject"]["email"] == "subj@test.com"
        assert len(data["journey_events"]) == 1
        assert data["comments"][0]["body"] == "看这里"
        assert data["subject"]["user"]["email"] == "subj@test.com"
        assert "个人数据" in data["note"]

    def test_erase_dry_run_then_anonymize(self, env):
        import asyncio
        from insflow.engine.privacy import erase_subject, export_subject

        async def _run():
            store = env["store"]
            await store.save_journey_event("test-ws", identity="gone@test.com",
                                           stage="visit", event="touch", props={})
            await store.add_comment("test-ws", "chart", "p1", "x",
                                    author="gone@test.com")
            dry = await erase_subject("test-ws", "gone@test.com", dry_run=True)
            done = await erase_subject("test-ws", "gone@test.com", dry_run=False)
            after = await export_subject("test-ws", "gone@test.com")
            logs = await store.list_admin_audit("test-ws", action="privacy.subject_erase")
            # 匿名化 = 解除身份关联：评论仍在库中但作者已匿名（不再归属该主体）
            row = await store._fetchone(
                "SELECT author, body FROM comments WHERE target_id = 'p1'")
            return dry, done, after, logs, dict(row or {})

        dry, done, after, logs, kept = \
            asyncio.get_event_loop().run_until_complete(_run())
        assert dry["dry_run"] and dry["plan"]["journey_events"] == 1
        assert done["dry_run"] is False
        assert after["journey_events"] == []                 # 身份已解除
        assert after["comments"] == []                       # 不再归属该主体
        assert kept["body"] == "x" and kept["author"] == "已删除用户"   # 内容保留、作者匿名
        assert logs and logs[0]["detail"]["mode"] == "anonymize"
        assert logs[0]["target_id"] and "@" not in logs[0]["target_id"]  # 不留完整邮箱

    def test_erase_purge(self, env):
        import asyncio
        from insflow.engine.privacy import erase_subject, export_subject

        async def _run():
            store = env["store"]
            await store.save_journey_event("test-ws", identity="p@test.com",
                                           stage="visit", event="touch", props={})
            await erase_subject("test-ws", "p@test.com", purge=True, dry_run=False)
            return await export_subject("test-ws", "p@test.com")

        data = asyncio.get_event_loop().run_until_complete(_run())
        assert data["journey_events"] == []

    def test_retention_sweep(self, env):
        import asyncio
        from insflow.engine.privacy import retention_sweep

        async def _run():
            store = env["store"]
            old = (datetime.now(UTC) - timedelta(days=400)).isoformat()
            fresh = datetime.now(UTC).isoformat()
            for i, ts in enumerate((old, fresh)):
                await store._execute(
                    """INSERT INTO metrics (id, workspace_id, entity_type, entity_id,
                       metric, value, dim_json, ts) VALUES (?, 'test-ws', 'site', 'x',
                       'm', 1, '{}', ?)""", (f"r{i}", ts))
            await store._db.commit()
            dry = await retention_sweep("test-ws", dry_run=True,
                                        override={"metrics_days": 180})
            done = await retention_sweep("test-ws", dry_run=False,
                                         override={"metrics_days": 180})
            row = await store._fetchone(
                "SELECT COUNT(*) AS n FROM metrics WHERE workspace_id = 'test-ws'")
            return dry, done, int((row or {}).get("n") or 0)

        dry, done, left = asyncio.get_event_loop().run_until_complete(_run())
        assert dry["result"]["metrics"]["would_delete"] == 1
        assert done["result"]["metrics"]["deleted"] == 1
        assert left == 1                                     # 未过期数据保留
        # insights 默认不清理（业务资产）
        assert done["result"]["insights"]["days"] == 0

    def test_privacy_apis_and_cli(self, env):
        import asyncio
        from click.testing import CliRunner
        from fastapi.testclient import TestClient
        from insflow.cli import main
        from insflow.server.app import app

        store = env["store"]
        asyncio.get_event_loop().run_until_complete(
            store.add_comment("test-ws", "chart", "p1", "x", author="api@test.com"))
        cli = TestClient(app)
        assert cli.get("/api/v1/privacy/subject/export",
                       params={"workspace_id": "test-ws",
                               "email": "api@test.com"}).status_code == 200
        assert cli.post("/api/v1/privacy/subject/erase",
                        json={"workspace_id": "test-ws", "email": "api@test.com",
                              "dry_run": True}).json()["dry_run"] is True
        assert cli.post("/api/v1/privacy/retention/sweep",
                        json={"workspace_id": "test-ws", "dry_run": True}).status_code == 200
        runner = CliRunner()
        res = runner.invoke(main, ["privacy", "retention", "-w", "test-ws"])
        assert res.exit_code == 0, res.output
        res2 = runner.invoke(main, ["privacy", "erase", "-w", "test-ws",
                                    "-e", "api@test.com"])
        assert res2.exit_code == 0 and "dry_run" in res2.output

    def test_retention_scheduled(self):
        src = open("insflow/core/bootstrap.py", encoding="utf-8").read()
        assert "privacy.retention" in src and "privacy.retention@sun-03:30" in src


class TestDeployAndTemplatePack:
    def test_template_export_from_workspace(self, env):
        import asyncio
        from insflow.engine.template_pack import export_from_workspace

        async def _run():
            store = env["store"]
            await store.create_monitor("test-ws", "keyword",
                                       {"queries": ["增长自动化"]},
                                       "0 */6 * * *")
            await store.create_monitor("test-ws", "site_change",
                                       {"urls": ["https://example.com/pricing"]},
                                       "0 */12 * * *")
            return await export_from_workspace("test-ws", template_id="tpl-x",
                                               name="增长行业包", industry="saas")

        res = asyncio.get_event_loop().run_until_complete(_run())
        assert res["errors"] == [] and res["monitors"] == 2
        spec = res["spec"]
        assert spec["id"] == "tpl-x" and spec["industry"] == "saas"
        assert {m["kind"] for m in spec["monitors"]} == {"keyword", "site_change"}
        # 幂等性：导出的模板应能通过校验（可直接给下一个客户 apply）
        from insflow.engine.template_pack import validate_template
        assert validate_template(spec) == []
        # 脱敏：不含数据行/凭据字段
        blob = json.dumps(spec, ensure_ascii=False)
        for leaked in ("password", "api_key", "token", "metrics", "insights"):
            assert leaked not in blob

    def test_template_export_cli(self, env):
        import asyncio
        import tempfile
        from pathlib import Path
        from click.testing import CliRunner
        from insflow.cli import main

        asyncio.get_event_loop().run_until_complete(
            env["store"].create_monitor("test-ws", "keyword", {}, "0 */6 * * *"))
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "pack.json")
            res = CliRunner().invoke(main, ["template", "export", "-w", "test-ws",
                                            "-o", out, "--name", "包"])
            assert res.exit_code == 0, res.output
            spec = json.loads(Path(out).read_text(encoding="utf-8"))
            assert spec["monitors"]
            ok = CliRunner().invoke(main, ["template", "validate", out])
            assert ok.exit_code == 0
            bad = Path(tmp) / "bad.json"
            bad.write_text('{"id": "x", "monitors": [{"kind": "非法"}]}')
            assert CliRunner().invoke(main, ["template", "validate", str(bad)]).exit_code != 0

    def test_deploy_scripts_present_and_valid(self):
        import subprocess
        from pathlib import Path
        for name in ("install.sh", "restore-drill.sh", "upgrade.sh"):
            path = Path("deploy") / name
            assert path.exists(), name
            # bash 语法检查（CI 里能挡住脚本改动引入的低级错误）
            proc = subprocess.run(["bash", "-n", str(path)], capture_output=True)
            assert proc.returncode == 0, proc.stderr.decode()[-300:]
        install = Path("deploy/install.sh").read_text(encoding="utf-8")
        assert "--with-systemd" in install and "db migrate" in install
        assert "doctor" in install and "template apply" in install
        drill = Path("deploy/restore-drill.sh").read_text(encoding="utf-8")
        assert "SLA 目标 < 600s" in drill and "backup" in drill

    def test_env_example_exists(self):
        text = open(".env.example", encoding="utf-8").read()
        assert "INSFLOW_MASTER_KEY" in text


class TestSnapshots:
    def _seed(self, env):
        import asyncio
        from insflow.engine.demo import DemoSeeder
        asyncio.get_event_loop().run_until_complete(
            DemoSeeder("test-ws", days=20).seed())

    def test_build_snapshot_self_contained(self, env):
        import asyncio
        from insflow.engine.snapshot import build_snapshot_html

        self._seed(env)
        html = asyncio.get_event_loop().run_until_complete(
            build_snapshot_html("test-ws", "board:traffic", days=20))
        assert "<!DOCTYPE html>" in html and "自包含快照" in html
        assert "<svg" in html                     # 图表内联
        assert "dp-table" in html                 # 数据表（可复制/可读屏幕阅读器）
        # 无外部依赖（不许出现 CDN / 外链脚本）
        for bad in ("https://cdn", "http://cdn", "<script src="):
            assert bad not in html

    def test_create_list_resolve_and_cleanup(self, env, monkeypatch, tmp_path):
        import asyncio
        import os
        import time
        from insflow.engine import snapshot as snap

        self._seed(env)
        monkeypatch.setattr(snap, "snapshot_dir",
                            lambda ws: (tmp_path / "snaps" / ws))
        (tmp_path / "snaps" / "test-ws").mkdir(parents=True)
        res = asyncio.get_event_loop().run_until_complete(
            snap.create_snapshot("test-ws", "board:traffic", days=20,
                                 make_pdf=False, title="流量周报"))
        assert res["size_kb"] > 0 and res["html_path"].endswith(".html")
        items = snap.list_snapshots("test-ws")
        assert items and items[0]["name"].endswith(".html")
        name = items[0]["name"]
        assert snap.resolve_snapshot("test-ws", name) is not None
        # 目录穿越必须被拒
        assert snap.resolve_snapshot("test-ws", "../evil.html") is None
        assert snap.resolve_snapshot("test-ws", "/etc/passwd") is None
        # 过期清理
        old = time.time() - 100 * 86400
        os.utime(snap.resolve_snapshot("test-ws", name), (old, old))
        out = snap.cleanup("test-ws", keep_days=90)
        assert out["removed"] == 1 and snap.list_snapshots("test-ws") == []

    def test_pdf_skipped_without_chrome(self, env, monkeypatch, tmp_path):
        import asyncio
        from insflow.engine import snapshot as snap

        self._seed(env)
        monkeypatch.setattr(snap, "snapshot_dir",
                            lambda ws: (tmp_path / "s2" / ws))
        (tmp_path / "s2" / "test-ws").mkdir(parents=True)
        monkeypatch.setenv("INSFLOW_CHROME_BIN", "")
        monkeypatch.setattr(snap, "chrome_bin", lambda: "")
        res = asyncio.get_event_loop().run_until_complete(
            snap.create_snapshot("test-ws", "metric:ga4_sessions", days=20))
        assert res.get("pdf_skipped") and "Chrome" in res["pdf_skipped"]
        assert "pdf_path" not in res                # 不伪造 PDF

    def test_push_uses_notify(self, env, monkeypatch, tmp_path):
        import asyncio
        from insflow.engine import snapshot as snap

        self._seed(env)
        monkeypatch.setattr(snap, "snapshot_dir",
                            lambda ws: (tmp_path / "s3" / ws))
        (tmp_path / "s3" / "test-ws").mkdir(parents=True)
        sent = []

        async def fake_notify(ws, title, summary, routes, escalation):
            sent.append((title, summary))
            return {"ok": True}
        monkeypatch.setattr("insflow.engine.alerts._notify", fake_notify)
        res = asyncio.get_event_loop().run_until_complete(
            snap.push_snapshot("test-ws", "board:journey", days=20, title="旅程周报"))
        assert res["link"].startswith("/console/snapshots/")
        assert sent and "旅程周报" in sent[0][0]

    def test_snapshot_api_and_page(self, env, monkeypatch, tmp_path):
        import asyncio
        from fastapi.testclient import TestClient
        from insflow.engine import snapshot as snap
        from insflow.server.app import app

        self._seed(env)
        monkeypatch.setattr(snap, "snapshot_dir",
                            lambda ws: (tmp_path / "s4" / ws))
        (tmp_path / "s4" / "test-ws").mkdir(parents=True)
        cli = TestClient(app)
        r = cli.post("/api/v1/snapshots", json={
            "workspace_id": "test-ws", "panel": "board:traffic", "days": 20,
            "pdf": False})
        assert r.status_code == 200 and r.json()["size_kb"] > 0
        assert cli.get("/api/v1/snapshots",
                       params={"workspace_id": "test-ws"}).json()["snapshots"]
        page = cli.get("/console/snapshots", params={"workspace_id": "test-ws"})
        assert page.status_code == 200 and "快照归档" in page.text
        # 文件下载（含防穿越）
        name = r.json()["html_rel"]
        file = cli.get(f"/console/snapshots/{name}", params={"workspace_id": "test-ws"})
        assert file.status_code == 200 and "自包含快照" in file.text
        assert cli.get("/console/snapshots/test-ws/../evil.html",
                       params={"workspace_id": "test-ws"}).status_code in (404, 400)
        # 审计留痕
        logs = cli.get("/api/v1/audit/admin",
                       params={"workspace_id": "test-ws"}).json()["logs"]
        assert any(x["action"] == "snapshot.create" for x in logs)

    def test_snapshot_cli_and_subscription(self, env, monkeypatch, tmp_path):
        import asyncio
        from click.testing import CliRunner
        from insflow.cli import main
        from insflow.engine import snapshot as snap
        from insflow.engine.subscriptions import SubscriptionService

        self._seed(env)
        monkeypatch.setattr(snap, "snapshot_dir",
                            lambda ws: (tmp_path / "s5" / ws))
        (tmp_path / "s5" / "test-ws").mkdir(parents=True)
        res = CliRunner().invoke(main, ["snapshot", "-w", "test-ws",
                                        "-p", "board:traffic", "--no-pdf"])
        assert res.exit_code == 0 and "快照已生成" in res.output

        async def _sub():
            svc = SubscriptionService("test-ws")
            await svc.create_snapshot("快照订阅", "board:traffic", ["webhook"],
                                      {"webhook_url": "https://example.com/x"},
                                      days=20)
            return await svc.dispatch_snapshots()
        out = asyncio.get_event_loop().run_until_complete(_sub())
        assert out["sent"] + out["failed"] >= 1

    def test_snapshot_schedule_registered(self):
        src = open("insflow/core/bootstrap.py", encoding="utf-8").read()
        assert "snapshot.cleanup@sun-04:00" in src
        assert "dispatch_snapshots" in src
