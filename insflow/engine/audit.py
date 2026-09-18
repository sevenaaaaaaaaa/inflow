"""Insight Flow 审计导出（R4-2，企业采购合规）

把事件流（events.jsonl）导出为采购方可审的 CSV/JSON：
- 过滤：时间范围 / 事件类型 / 关键字
- 覆盖：登录、密钥变更、插件安装、动作派发、外部推送、数据导出、配额熔断
- 导出动作本身也写审计（audit.exported），形成闭环
"""

import csv
import io
import json
from datetime import UTC, datetime
from pathlib import Path

from ..core.files import EventBus

# 审计关注的事件类别（企业合规清单）
AUDIT_CATEGORIES = {
    "认证": ("account.", "auth."),
    "凭据": ("source.credentials_saved", "source.token_"),
    "插件": ("plugin.", "template.applied"),
    "动作": ("action.", "feedback."),
    "数据": ("data.exported", "backup.", "ingest."),
    "配额": ("quota."),
    "系统": ("monitor.", "insight.created", "report.ready", "task."),
}


def categorize(event_type: str) -> str:
    for cat, prefixes in AUDIT_CATEGORIES.items():
        if any(event_type.startswith(p) for p in prefixes):
            return cat
    return "其他"


class AuditExporter:
    """事件流审计导出器"""

    def __init__(self, workspace_id: str = "default"):
        self.workspace_id = workspace_id
        self.bus = EventBus(workspace_id)

    def collect(self, date_from: str | None = None, date_to: str | None = None,
                event_type: str | None = None, keyword: str | None = None) -> list[dict]:
        """读取并按条件过滤事件（时间/类型/关键字）"""
        events = self.bus.read(limit=100000)
        out = []
        for e in events:
            ts = e.get("ts", "")
            if date_from and ts < date_from:
                continue
            if date_to and ts > date_to + "T23:59:59":
                continue
            if event_type and not e.get("type", "").startswith(event_type):
                continue
            if keyword:
                blob = json.dumps(e, ensure_ascii=False)
                if keyword not in blob:
                    continue
            out.append(e)
        return out

    def export_csv(self, events: list[dict]) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["时间", "类别", "事件", "工作区", "摘要"])
        for e in events:
            payload = e.get("payload", {})
            summary = json.dumps(payload, ensure_ascii=False)[:300]
            writer.writerow([e.get("ts", ""), categorize(e.get("type", "")),
                             e.get("type", ""), e.get("workspace", ""), summary])
        return buf.getvalue()

    def export_json(self, events: list[dict]) -> str:
        return json.dumps({
            "exported_at": datetime.now(UTC).isoformat(),
            "workspace_id": self.workspace_id,
            "count": len(events),
            "events": events,
        }, ensure_ascii=False, indent=2, default=str)

    def run(self, out_path: str | Path, fmt: str = "csv",
            date_from: str | None = None, date_to: str | None = None,
            event_type: str | None = None, keyword: str | None = None) -> dict:
        """执行导出（同时写审计事件）"""
        events = self.collect(date_from, date_to, event_type, keyword)
        content = self.export_csv(events) if fmt == "csv" else self.export_json(events)
        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

        self.bus.emit("audit.exported", {
            "path": str(path), "format": fmt, "count": len(events),
            "date_from": date_from, "date_to": date_to,
        })
        return {"ok": True, "path": str(path), "count": len(events), "format": fmt}
