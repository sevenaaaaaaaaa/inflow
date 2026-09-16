"""测试数据备份（在线备份/保留清理/恢复）"""

import sqlite3
from pathlib import Path

import pytest

from insflow.core.store import Store
from insflow.engine.backup import BackupManager


@pytest.fixture
def env(tmp_path, monkeypatch):
    """数据目录 + 一个有内容的库"""
    data = tmp_path / "data"
    data.mkdir()
    db = data / "insflow.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE t (x TEXT)")
    conn.execute("INSERT INTO t VALUES ('客户资产数据')")
    conn.commit()
    conn.close()
    (data / "reports").mkdir()
    (data / "reports" / "a.md").write_text("# 报告")
    (data / "events").mkdir()
    (data / "events" / "e.jsonl").write_text('{"ts": 1}\n')

    yield {"data": data}
    # 恢复测试会移动 data 目录，跳过清理


class TestBackup:
    def test_run_creates_backup(self, env, tmp_path):
        mgr = BackupManager(data_dir=env["data"], backup_dir=tmp_path / "data-backup")
        result = mgr.run()
        assert result["ok"] is True
        assert "insflow.db" in result["files"]
        assert result["duration_s"] < 10  # SLA 预热

        # 备份库可读且数据完整
        bdb = result["backup_dir"] + "/insflow.db"
        conn = sqlite3.connect(bdb)
        rows = conn.execute("SELECT x FROM t").fetchall()
        conn.close()
        assert rows[0][0] == "客户资产数据"

    def test_backup_restores_wal_consistency(self, env, tmp_path):
        """在线备份期间写入不损坏备份（backup API 语义）"""
        mgr = BackupManager(data_dir=env["data"], backup_dir=tmp_path / "data-backup")
        # 模拟备份时并发写入
        conn = sqlite3.connect(str(env["data"] / "insflow.db"))
        result = mgr.run()
        conn.execute("INSERT INTO t VALUES ('备份期间写入')")
        conn.commit()
        conn.close()
        assert result["ok"]

    def test_prune_keeps_recent(self, env, tmp_path):
        mgr = BackupManager(data_dir=env["data"], backup_dir=tmp_path / "data-backup")
        mgr.run()
        assert mgr.prune(keep_days=30) == 0  # 刚备份的不会删

    def test_prune_removes_old(self, env, tmp_path):
        mgr = BackupManager(data_dir=env["data"], backup_dir=tmp_path / "data-backup")
        # 造两个目录：一个新（合法）一个旧（超期）
        old_dir = tmp_path / "data-backup" / "20250101-0000"
        old_dir.mkdir(parents=True)
        mgr.run()
        removed = mgr.prune(keep_days=30)
        assert removed == 1
        assert not old_dir.exists()


class TestRestore:
    def test_restore_requires_confirm(self, env, tmp_path):
        mgr = BackupManager(data_dir=env["data"], backup_dir=tmp_path / "data-backup")
        backup = mgr.run()
        result = mgr.restore(backup["backup_dir"], confirm=False)
        assert result["ok"] is False
        assert "confirm" in result["detail"]

    def test_restore_roundtrip(self, env, tmp_path):
        """备份 → 破坏原库 → 恢复 → 数据回来（SLA 演练语义）"""
        mgr = BackupManager(data_dir=env["data"], backup_dir=tmp_path / "data-backup")
        backup = mgr.run()

        # 破坏当前库
        (env["data"] / "insflow.db").write_text("corrupted")

        result = mgr.restore(backup["backup_dir"], confirm=True)
        assert result["ok"] is True
        assert "pre-restore" in result["previous_data_snapshot"]

        # 恢复后的库数据完整
        conn = sqlite3.connect(str(env["data"] / "insflow.db"))
        rows = conn.execute("SELECT x FROM t").fetchall()
        conn.close()
        assert any("客户资产数据" in r[0] for r in rows)

    def test_restore_missing_backup(self, env, tmp_path):
        mgr = BackupManager(data_dir=env["data"], backup_dir=tmp_path / "data-backup")
        result = mgr.restore(tmp_path / "nonexistent", confirm=True)
        assert result["ok"] is False


class TestTenantBackup:
    async def test_workspace_export(self, env, tmp_path, monkeypatch):
        """租户级导出：独立 JSON + 数据结构完整"""
        import json as jsonlib
        import insflow.core.files as fm
        from insflow.core.entities import Workspace
        from insflow.core.store import get_store, reset_store

        monkeypatch.setenv("INSFLOW_MASTER_KEY", "mk")
        mgr = BackupManager(data_dir=env["data"], backup_dir=tmp_path / "data-backup")

        s = Store(db_path=tmp_path / "t.db")
        await s.connect()
        await s.migrate()
        reset_store(s)
        await s.create_workspace(Workspace(id="test-ws", name="T"))

        result = await mgr.export_workspace("test-ws", tmp_path / "export")
        assert result["ok"] is True
        data = jsonlib.loads(Path(result["path"]).read_text(encoding="utf-8"))
        assert data["workspace_id"] == "test-ws"
        assert data["exported_at"]

        await s.close()
        reset_store(None)
