"""批次 B：开放度（事件目录 / 插件脚手架 / examples / OpenAPI 契约）"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from insflow.cli import main
from insflow.core.event_catalog import (
    DOC_PATH,
    EVENTS,
    catalog_inbound_names,
    catalog_outbound_names,
    ingest_handler_types,
    outbound_action_types,
    render_markdown,
)
from insflow.core.openapi_contract import SNAPSHOT_PATH, check_snapshot
from insflow.engine.marketplace import check_plugin
from insflow.engine.plugin_scaffold import create_plugin
from insflow.server.app import app

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def client(tmp_path, monkeypatch):
    import insflow.core.files as files_mod
    from insflow.core.store import reset_store
    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)
    monkeypatch.setenv("INSFLOW_DISABLE_SCHEDULER", "1")
    monkeypatch.setenv("INSFLOW_MASTER_KEY", "batch-b")
    reset_store(None)
    with TestClient(app) as c:
        yield c
    reset_store(None)


class TestEventCatalog:
    def test_api_shape(self, client):
        r = client.get("/api/v1/events/catalog")
        assert r.status_code == 200
        body = r.json()
        assert body["schema_version"] == "1.0"
        assert body["count"] == len(EVENTS)
        ev = body["events"][0]
        for key in ("type", "direction", "channel", "required", "example"):
            assert key in ev
        inbound = client.get("/api/v1/events/catalog", params={"direction": "inbound"})
        assert inbound.status_code == 200
        assert all(e["direction"] == "inbound" for e in inbound.json()["events"])
        assert client.get("/api/v1/events/catalog",
                          params={"direction": "nope"}).status_code == 400

    def test_covers_ingest_handlers(self):
        missing = ingest_handler_types() - catalog_inbound_names()
        assert not missing, f"入站 handler 未进目录: {missing}"

    def test_covers_action_types(self):
        missing = outbound_action_types() - catalog_outbound_names()
        assert not missing, f"出站动作未进目录: {missing}"

    def test_docs_in_sync(self):
        assert DOC_PATH.exists(), "缺少 docs/14-事件目录.md，请跑 insflow events catalog --write"
        assert DOC_PATH.read_text(encoding="utf-8") == render_markdown()

    def test_cli_json(self):
        res = CliRunner().invoke(main, ["events", "catalog", "--json"])
        assert res.exit_code == 0, res.output
        data = json.loads(res.output)
        assert data["count"] == len(EVENTS)


class TestPluginScaffold:
    def test_all_types_pass_check(self, tmp_path):
        for ptype in ("source", "model", "action", "template"):
            dest = tmp_path / ptype / f"demo-{ptype}"
            result = create_plugin(ptype, f"demo-{ptype}", dest=dest, name="Demo")
            assert result["ok"], result
            assert result["check"]["passed"], result["check"]
            assert check_plugin(dest)["passed"]

    def test_rejects_bad_id(self, tmp_path):
        result = create_plugin("source", "Bad_ID", dest=tmp_path / "Bad_ID")
        assert result["ok"] is False
        assert "小写" in result["error"]

    def test_refuse_overwrite_without_force(self, tmp_path):
        dest = tmp_path / "once"
        assert create_plugin("source", "once", dest=dest)["ok"]
        again = create_plugin("source", "once", dest=dest)
        assert again["ok"] is False
        forced = create_plugin("source", "once", dest=dest, force=True)
        assert forced["ok"] and forced["check"]["passed"]

    def test_cli_new(self, tmp_path):
        res = CliRunner().invoke(main, [
            "plugin", "new", "source", "cli-demo", "--out", str(tmp_path),
        ])
        assert res.exit_code == 0, res.output
        assert (tmp_path / "cli-demo" / "manifest.json").exists()
        check = CliRunner().invoke(main, ["plugin", "check", str(tmp_path / "cli-demo")])
        assert check.exit_code == 0, check.output
        assert "PASSED" in check.output

    def test_builtin_serper_still_passes(self):
        report = check_plugin(REPO / "plugins" / "sources" / "serper")
        assert report["passed"], report["errors"]


class TestExamples:
    def test_required_files_exist(self):
        required = [
            "examples/README.md",
            "examples/curl/list-insights.sh",
            "examples/curl/ingest.sh",
            "examples/curl/catalog.sh",
            "examples/python/list_insights.py",
            "examples/python/ingest_event.py",
            "examples/typescript/list-insights.ts",
            "examples/n8n/weekly-report.json",
            "examples/dify/insight-agent.json",
            "examples/mcp/claude_desktop.json",
            "examples/mcp/cursor.json",
            "docs/15-插件开发指南.md",
        ]
        missing = [p for p in required if not (REPO / p).exists()]
        assert not missing, missing

    def test_examples_use_placeholders(self):
        text = (REPO / "examples/README.md").read_text(encoding="utf-8")
        assert "IF_BASE" in text and "IF_WORKSPACE" in text
        n8n = json.loads((REPO / "examples/n8n/weekly-report.json").read_text())
        assert "nodes" in n8n and n8n["nodes"]
        mcp = json.loads((REPO / "examples/mcp/cursor.json").read_text())
        assert mcp["mcpServers"]["insight-flow"]["args"] == ["mcp"]


class TestOpenApiContract:
    def test_snapshot_locked(self):
        assert SNAPSHOT_PATH.exists(), "缺少 contracts/openapi.json，请跑 python scripts/export_openapi.py --write"
        report = check_snapshot()
        assert report["ok"], report.get("reason")

    def test_catalog_path_in_spec(self):
        from insflow.core.openapi_contract import dump_spec
        spec = json.loads(dump_spec())
        assert "/api/v1/events/catalog" in spec["paths"]


class TestMcpCli:
    def test_help_lists_mcp(self):
        res = CliRunner().invoke(main, ["mcp", "--help"])
        assert res.exit_code == 0
        assert "stdio" in res.output
