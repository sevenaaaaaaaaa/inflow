"""测试审计导出（R4-2）"""

import csv
import io
import json

import pytest

import insflow.core.files as files_mod
from insflow.core.files import EventBus
from insflow.engine.audit import AuditExporter, categorize


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)
    bus = EventBus("test-ws")
    bus.emit("account.login", {"email": "a@x.com"})
    bus.emit("auth.key_created", {"key_id": "k1"})
    bus.emit("action.dispatched", {"action_id": "a1"})
    bus.emit("quota.exceeded", {"source": "serper"})
    bus.emit("data.exported", {"format": "json"})
    bus.emit("insight.created", {"title": "t"})
    return {"bus": bus}


class TestCategorize:
    def test_categories(self):
        assert categorize("account.login") == "认证"
        assert categorize("auth.key_created") == "认证"
        assert categorize("action.dispatched") == "动作"
        assert categorize("quota.exceeded") == "配额"
        assert categorize("data.exported") == "数据"
        assert categorize("plugin.installed") == "插件"
        assert categorize("random.thing") == "其他"


class TestExport:
    def test_csv_export(self, env, tmp_path):
        exporter = AuditExporter("test-ws")
        out = tmp_path / "audit.csv"
        result = exporter.run(out, fmt="csv")
        assert result["ok"] and result["count"] == 6

        rows = list(csv.reader(io.StringIO(out.read_text(encoding="utf-8"))))
        assert rows[0][0] == "时间" and rows[0][1] == "类别"
        assert len(rows) == 7  # 表头 + 6 条

    def test_json_export(self, env, tmp_path):
        out = tmp_path / "audit.json"
        AuditExporter("test-ws").run(out, fmt="json")
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["count"] == 6
        assert data["workspace_id"] == "test-ws"

    def test_filter_by_type(self, env, tmp_path):
        events = AuditExporter("test-ws").collect(event_type="quota.")
        assert len(events) == 1
        assert events[0]["type"] == "quota.exceeded"

    def test_filter_by_keyword(self, env, tmp_path):
        events = AuditExporter("test-ws").collect(keyword="serper")
        assert len(events) == 1

    def test_filter_by_date(self, env, tmp_path):
        assert AuditExporter("test-ws").collect(date_from="2999-01-01") == []
        assert len(AuditExporter("test-ws").collect(date_to="2999-12-31")) == 6

    def test_export_itself_audited(self, env, tmp_path):
        """导出动作本身写审计（形成闭环）"""
        AuditExporter("test-ws").run(tmp_path / "a.csv", fmt="csv")
        events = AuditExporter("test-ws").collect(event_type="audit.exported")
        assert len(events) == 1
        assert events[0]["payload"]["format"] == "csv"
