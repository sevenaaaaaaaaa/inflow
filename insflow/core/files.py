"""Insight Flow 文件存储约定

与 MFlow 一致的"报告即文件"哲学：
data/
├── reports/{workspace}/diagnosis/2026-W38.md      # 诊断周报
├── reports/{workspace}/competitors/2026-09-14.md  # 竞品周报
├── reports/{workspace}/maturity/2026-09.md        # 成熟度报告
├── snapshots/{workspace}/{monitor_id}/2026-09-16/ # 网页快照与 diff
└── events/{workspace}/events.jsonl                # 事件流（审计+回放）
"""

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# 项目根目录（insflow 包的上上级）
ROOT_DIR = Path(__file__).parent.parent.parent
DATA_DIR = ROOT_DIR / "data"


def _now() -> datetime:
    return datetime.now(UTC)


class EventBus:
    """事件流总线

    不可变追加的 JSONL 文件，用于：
    1. 审计（登录/密钥变更/插件安装/动作派发/外部推送）
    2. 事件驱动编排（采集完成 → 触发模型 → 触发报告/告警）
    3. 回放（调试与复盘）
    """

    def __init__(self, workspace_id: str = "default", base_dir: Path | None = None):
        self.workspace_id = workspace_id
        base = base_dir or DATA_DIR
        self.events_dir = base / "events" / workspace_id
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self.events_file = self.events_dir / "events.jsonl"

    def emit(self, event_type: str, payload: dict | None = None) -> dict:
        """写入一条事件（不可变追加）

        事件类型约定：
        - monitor.run_started / monitor.run_finished / monitor.alert
        - model.run_started / model.run_finished
        - insight.created / insight.acknowledged / insight.dismissed
        - action.dispatched / action.executed / action.failed
        - feedback.received
        - quota.warning / quota.exceeded
        - source.collected
        - report.ready
        """
        event = {
            "ts": _now().isoformat(),
            "type": event_type,
            "workspace": self.workspace_id,
            "payload": payload or {},
        }
        line = json.dumps(event, ensure_ascii=False, default=str)
        with open(self.events_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return event

    def read(
        self,
        event_type: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """读取事件（可选过滤）"""
        if not self.events_file.exists():
            return []
        events = []
        with open(self.events_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event_type and event.get("type") != event_type:
                    continue
                if since and event.get("ts", "") < since.isoformat():
                    continue
                events.append(event)
        return events[-limit:]


    # ========== 轮转（性能守则：事件流不得无限增长）==========

    def rotate(self, keep_days: int = 30) -> dict:
        """按时间裁剪事件流，保留最近 N 天

        教训（OpenFlow）：生产曾积累 63 万行无意义事件（heartbeat/scroll）导致查询慢、
        存储膨胀。事件流是审计与编排的底座，必须设置保留期。
        """
        from datetime import datetime, timedelta, timezone
        if not self.events_file.exists():
            return {"kept": 0, "dropped": 0}
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        kept, dropped = [], 0
        with open(self.events_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("ts", "") >= cutoff:
                    kept.append(line)
                else:
                    dropped += 1
        if dropped:
            tmp = self.events_file.with_suffix(".jsonl.tmp")
            tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
            tmp.replace(self.events_file)
        return {"kept": len(kept), "dropped": dropped}


class ReportStore:
    """报告文件存储"""

    def __init__(self, workspace_id: str, base_dir: Path | None = None):
        self.workspace_id = workspace_id
        self.base = (base_dir or DATA_DIR) / "reports" / workspace_id
        self.base.mkdir(parents=True, exist_ok=True)

    def _iso_week(self, dt: datetime) -> str:
        return f"{dt.isocalendar().year}-W{dt.isocalendar().week:02d}"

    def save_report(self, category: str, content: str, dt: datetime | None = None) -> Path:
        """保存报告

        category: diagnosis | competitors | maturity | deep-dive
        文件名按类别用不同周期：周报用 ISO 周，月报用月份
        """
        dt = dt or _now()
        cat_dir = self.base / category
        cat_dir.mkdir(parents=True, exist_ok=True)

        if category in ("maturity",):
            filename = f"{dt.strftime('%Y-%m')}.md"
        elif category in ("diagnosis", "competitors"):
            filename = f"{self._iso_week(dt)}.md"
        else:
            filename = f"{dt.strftime('%Y-%m-%d')}.md"

        path = cat_dir / filename
        path.write_text(content, encoding="utf-8")
        return path

    def list_reports(self, category: str) -> list[Path]:
        """列出某类报告"""
        cat_dir = self.base / category
        if not cat_dir.exists():
            return []
        return sorted(cat_dir.glob("*.md"), reverse=True)

    def get_report(self, category: str, filename: str) -> str | None:
        """读取报告内容"""
        path = self.base / category / filename
        if path.exists():
            return path.read_text(encoding="utf-8")
        return None


class SnapshotStore:
    """网页快照存储

    data/snapshots/{workspace}/{monitor_id}/YYYY-MM-DD/content.html
    """

    def __init__(self, workspace_id: str, base_dir: Path | None = None):
        self.workspace_id = workspace_id
        self.base = (base_dir or DATA_DIR) / "snapshots" / workspace_id

    def save_snapshot(self, monitor_id: str, content: str, dt: datetime | None = None) -> Path:
        """保存网页快照"""
        dt = dt or _now()
        snap_dir = self.base / monitor_id / dt.strftime("%Y-%m-%d")
        snap_dir.mkdir(parents=True, exist_ok=True)
        path = snap_dir / "content.html"
        path.write_text(content, encoding="utf-8")
        return path

    def get_latest_snapshot(self, monitor_id: str) -> tuple[Path, str] | None:
        """获取最近一次快照"""
        mon_dir = self.base / monitor_id
        if not mon_dir.exists():
            return None
        days = sorted([d for d in mon_dir.iterdir() if d.is_dir()], reverse=True)
        for day_dir in days:
            path = day_dir / "content.html"
            if path.exists():
                return path, path.read_text(encoding="utf-8")
        return None

    def list_snapshots(self, monitor_id: str) -> list[Path]:
        """列出快照日期"""
        mon_dir = self.base / monitor_id
        if not mon_dir.exists():
            return []
        return sorted([d for d in mon_dir.iterdir() if d.is_dir()], reverse=True)


def atomic_write_json(path: Path, data: Any) -> None:
    """原子写入 JSON 文件"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, str(path))
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
