"""Insight Flow 数据备份（R1-2）

data/ 是客户资产（SQLite 库 + 报告 + 快照 + 事件流）。备份三件套：
1. `insflow backup` 命令 / `BackupManager.run()`：SQLite 在线 checkpoint 备份（WAL 安全）
   + 报告/事件流目录打包 → data-backup/YYYYMMDD-HHMM/
2. 保留策略：默认保留 30 天，超期清理
3. systemd daily timer 挂接（deploy/systemd/insflow-daily.timer 已有，任务侧调用本模块）
"""

import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..core.files import DATA_DIR, EventBus, atomic_write_json
from ..core.store import get_store

ROOT_DIR = Path(__file__).parent.parent.parent
DEFAULT_BACKUP_DIR = ROOT_DIR / "data-backup"


class BackupManager:
    """数据备份（在线 checkpoint + 保留清理）"""

    def __init__(self, data_dir: Path | None = None, backup_dir: Path | None = None):
        self.data_dir = data_dir or DATA_DIR
        self.backup_dir = backup_dir or DEFAULT_BACKUP_DIR

    # ========== 主流程 ==========

    def run(self, db_name: str = "insflow.db") -> dict:
        """执行一次备份（SQLite 在线备份，不锁写）

        Returns:
            {"ok", "backup_dir", "db_size_bytes", "files", "duration_ms"}
        """
        started = datetime.now(UTC)
        stamp = started.strftime("%Y%m%d-%H%M")
        target = self.backup_dir / stamp
        target.mkdir(parents=True, exist_ok=True)

        files = []

        # 1. SQLite 在线备份（sqlite3 backup API，WAL 安全）
        db_path = self.data_dir / db_name
        if db_path.exists():
            out_db = target / db_name
            self._backup_sqlite(db_path, out_db)
            files.append((out_db.name, out_db.stat().st_size))

        # 2. 报告 / 事件流目录拷贝（快照目录可能大，按需包含）
        for sub in ("reports", "events"):
            src = self.data_dir / sub
            if src.exists():
                shutil.copytree(src, target / sub, dirs_exist_ok=True)
                files.append((sub, sum(f.stat().st_size for f in target.joinpath(sub).rglob("*"))))

        # 备份元数据
        atomic_write_json(target / "backup-meta.json", {
            "created_at": started.isoformat(),
            "db": db_name if db_path.exists() else None,
            "files": {name: size for name, size in files},
        })

        duration = (datetime.now(UTC) - started).total_seconds()
        EventBus("default").emit("backup.completed", {
            "backup_dir": str(target), "duration_s": round(duration, 2),
        })
        return {
            "ok": True,
            "backup_dir": str(target),
            "files": [name for name, _ in files],
            "duration_s": round(duration, 2),
        }

    def _backup_sqlite(self, db_path: Path, out_path: Path) -> None:
        """sqlite3 backup API（在线、WAL 安全）"""
        src = sqlite3.connect(str(db_path))
        dst = sqlite3.connect(str(out_path))
        try:
            src.backup(dst)
        finally:
            src.close()
            dst.close()

    # ========== 租户级备份/恢复（R3-4：多客户隔离场景）==========

    async def workspace_stats(self, workspace_id: str) -> dict:
        """租户数据统计（备份/恢复前预览）"""
        store = await get_store()
        insights = await store.list_insights(workspace_id, limit=10000)
        actions = await store.list_actions(workspace_id)
        return {"workspace_id": workspace_id, "insights": len(insights),
                "actions": len(actions)}

    async def export_workspace(self, workspace_id: str, out_dir: "Path | str") -> dict:
        """租户级导出：该 workspace 全量数据 → 独立 JSON（审计 + 隔离恢复）"""
        import json as jsonlib
        store = await get_store()
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        data = {
            "exported_at": datetime.now(UTC).isoformat(),
            "workspace_id": workspace_id,
            "insights": [i.model_dump(mode="json")
                         for i in await store.list_insights(workspace_id, limit=10000)],
            "actions": [a.model_dump(mode="json")
                        for a in await store.list_actions(workspace_id)],
            "feedback": await store.get_feedback_stats(workspace_id),
        }
        path = Path(out_dir) / f"ws-{workspace_id}.json"
        path.write_text(jsonlib.dumps(data, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
        EventBus(workspace_id).emit("backup.workspace_exported", {
            "path": str(path),
        })
        return {"ok": True, "path": str(path)}

    # ========== 保留策略 ==========

    def prune(self, keep_days: int = 30) -> int:
        """清理超过保留期的备份目录（返回删除数）"""
        cutoff = datetime.now(UTC) - timedelta(days=keep_days)
        removed = 0
        if not self.backup_dir.exists():
            return removed
        for d in self.backup_dir.iterdir():
            if not d.is_dir():
                continue
            try:
                dt = datetime.strptime(d.name, "%Y%m%d-%H%M").replace(tzinfo=UTC)
            except ValueError:
                continue  # 非备份目录
            if dt < cutoff:
                shutil.rmtree(d)
                removed += 1
        if removed:
            EventBus("default").emit("backup.pruned", {"removed": removed})
        return removed

    # ========== 恢复（SLA：<10 分钟）==========

    def restore(self, backup_path: str | Path, confirm: bool = False) -> dict:
        """从备份恢复 data/（危险操作，需 confirm=True 二次确认）

        流程：校验备份完整性 → data/ 置换为备份内容 → 提示重启服务
        """
        backup = Path(backup_path)
        db_backup = backup / "insflow.db"
        if not db_backup.exists():
            return {"ok": False, "detail": "备份不完整（缺 insflow.db）"}

        if not confirm:
            return {"ok": False, "detail": "恢复将覆盖当前 data/，请 confirm=True 重试"}

        # 先把当前 data 移走（可回退）
        now = datetime.now(UTC).strftime("%Y%m%d-%H%M")
        pre_restore = self.backup_dir / f"pre-restore-{now}"
        pre_restore.mkdir(parents=True, exist_ok=True)
        if self.data_dir.exists():
            shutil.move(str(self.data_dir), str(pre_restore / "data"))

        self.data_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(db_backup, self.data_dir / db_backup.name)
        for sub in ("reports", "events"):
            src = backup / sub
            if src.exists():
                shutil.copytree(src, self.data_dir / sub, dirs_exist_ok=True)

        EventBus("default").emit("backup.restored", {
            "from": str(backup), "pre_restore_snapshot": str(pre_restore / "data"),
        })
        return {
            "ok": True,
            "restored_from": str(backup),
            "previous_data_snapshot": str(pre_restore / "data"),
            "note": "请重启 insflow serve 使恢复生效",
        }
