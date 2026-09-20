"""测试双驱动（SQLite / MySQL）方言与后端选择（对齐 OpenFlow EventStore 教训）"""

import pytest

from insflow.core.db import (
    MySQLBackend,
    SQLiteBackend,
    create_backend,
    get_dialect,
    mysql_config_from_env,
    resolve_driver,
)
from insflow.core.db.schema_mysql import MYSQL_MIGRATIONS


class TestDialect:
    def test_sqlite_placeholder_unchanged(self):
        d = get_dialect("sqlite")
        assert d.adapt("SELECT * FROM t WHERE a = ?") == "SELECT * FROM t WHERE a = ?"

    def test_mysql_placeholder_converted(self):
        """教训 #1：占位符不能共用（? → %s）"""
        d = get_dialect("mysql")
        sql = d.adapt("SELECT * FROM t WHERE a = ? AND b = ?")
        assert sql.count("%s") == 2 and "?" not in sql

    def test_insert_or_ignore_per_driver(self):
        """教训 #1：INSERT OR IGNORE ↔ INSERT IGNORE"""
        assert get_dialect("sqlite").insert_or_ignore(
            "INSERT OR IGNORE INTO x VALUES (?)") == "INSERT OR IGNORE INTO x VALUES (?)"
        mysql_sql = get_dialect("mysql").insert_or_ignore("INSERT OR IGNORE INTO x VALUES (?)")
        assert mysql_sql.startswith("INSERT IGNORE") and "OR IGNORE" not in mysql_sql

    def test_date_bucket_per_driver(self):
        """教训 #2：strftime ↔ DATE_FORMAT（周格式不同）"""
        assert get_dialect("sqlite").bucket("ts", "%Y-%m-%d") == "strftime('%Y-%m-%d', ts)"
        assert get_dialect("mysql").bucket("ts", "%Y-%m-%d") == "DATE_FORMAT(ts, '%Y-%m-%d')"
        # 周：SQLite %W ↔ MySQL %x-W%v（ISO 年-周）
        assert get_dialect("mysql").bucket("ts", "%Y-W%W") == "DATE_FORMAT(ts, '%x-W%v')"

    def test_mysql_percent_escaping(self):
        """教训 #4：SQL 字面量 % 必须转义（否则 pymysql 参数替换报错）"""
        d = get_dialect("mysql")
        out = d.adapt("SELECT DATE_FORMAT(ts, '%Y-%m-%d') AS b FROM m WHERE a = ?")
        assert "%%Y-%%m-%%d" in out            # 字面量 % 已转义
        assert out.count("%s") == 1            # 占位符未被误伤
        rendered = out % ("x",)                # 模拟 pymysql：不应抛 ValueError
        assert "%Y-%m-%d" in rendered and "x" in rendered

    def test_partial_index_capability(self):
        """教训 #3：MySQL 5.7 无部分索引"""
        assert get_dialect("sqlite").supports_partial_index is True
        assert get_dialect("mysql").supports_partial_index is False


class TestDriverSelection:
    def test_default_sqlite(self, monkeypatch):
        monkeypatch.delenv("INSFLOW_DB_DRIVER", raising=False)
        assert resolve_driver() == "sqlite"
        assert isinstance(create_backend(), SQLiteBackend)

    def test_mysql_selected_by_env(self, monkeypatch):
        monkeypatch.setenv("INSFLOW_DB_DRIVER", "mysql")
        assert resolve_driver() == "mysql"
        assert isinstance(create_backend(), MySQLBackend)

    def test_mysql_config_from_env(self, monkeypatch):
        """命名对齐 OpenFlow 的 mysql_* 约定"""
        monkeypatch.setenv("MYSQL_HOST", "db.internal")
        monkeypatch.setenv("MYSQL_PORT", "3307")
        monkeypatch.setenv("MYSQL_DBNAME", "insflow")
        monkeypatch.setenv("MYSQL_USER", "insflow_u")
        monkeypatch.setenv("MYSQL_PASS", "secret")
        cfg = mysql_config_from_env()
        assert cfg["host"] == "db.internal" and cfg["port"] == 3307
        assert cfg["user"] == "insflow_u" and cfg["password"] == "secret"

    async def test_mysql_missing_config_fails_closed(self, monkeypatch):
        """配置不全 → 明确报错（fail-closed），不静默回落 SQLite"""
        monkeypatch.setenv("INSFLOW_DB_DRIVER", "mysql")
        monkeypatch.setenv("MYSQL_HOST", "")
        monkeypatch.setenv("MYSQL_DBNAME", "")
        backend = create_backend("mysql")
        with pytest.raises(RuntimeError) as e:
            await backend.connect()
        assert ("未安装" in str(e.value)) or ("配置不完整" in str(e.value))


class TestMySQLSchema:
    """DDL 结构断言（无 MySQL 服务器时的静态校验）"""

    def test_all_tables_present(self):
        ddl = " ".join(MYSQL_MIGRATIONS)
        for table in ("workspaces", "sources", "monitors", "raw_records", "metrics",
                      "insights", "actions", "feedback", "competitors",
                      "keyword_ranks", "journey_events", "users", "sessions",
                      "subscriptions"):
            assert f"CREATE TABLE IF NOT EXISTS {table}" in ddl, table

    def test_indexed_columns_are_varchar_not_text(self):
        """教训：MySQL 不能对裸 TEXT 建索引 → 索引列必须 VARCHAR"""
        for stmt in MYSQL_MIGRATIONS:
            for line in stmt.splitlines():
                if "INDEX" in line or "UNIQUE KEY" in line:
                    # 索引定义行不应引用 TEXT 列
                    assert " TEXT" not in line

    def test_no_partial_index_and_no_unique_on_maybe_empty(self):
        """教训 #3：MySQL 无部分索引，且**不能**对可能为空的列建普通唯一键
        （空值会互相冲突）→ 去重放代码层（collector_router._save_metrics 先查后插）"""
        ddl = " ".join(MYSQL_MIGRATIONS)
        assert "WHERE window_key" not in ddl
        assert "UNIQUE KEY" not in ddl or "metrics" not in ddl.split("UNIQUE KEY")[0][-200:]

    def test_engine_and_charset(self):
        for stmt in MYSQL_MIGRATIONS:
            if stmt.strip().upper().startswith("ALTER"):
                continue          # ALTER 语句没有 ENGINE/CHARSET 子句
            assert "ENGINE=InnoDB" in stmt and "utf8mb4" in stmt

    def test_no_sqlite_only_syntax(self):
        ddl = " ".join(MYSQL_MIGRATIONS)
        assert "PRAGMA" not in ddl
        assert "INSERT OR IGNORE" not in ddl
        assert "strftime" not in ddl


class TestMySQLVisibility:
    """回归：MySQL 长连接 REPEATABLE READ 快照会让外部写入不可见

    实测场景：CLI/运维脚本写了 pending 动作，控制台（服务进程）查不到 →
    因为 autocommit=0 下第一次 SELECT 的快照持续到本连接下次 COMMIT。
    修复：连接时 SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED。
    （仅 MySQL 环境执行；SQLite 下跳过）
    """

    async def test_external_write_visible_after_service_read(self, monkeypatch):
        import os
        if os.environ.get("INSFLOW_DB_DRIVER", "sqlite") != "mysql":
            pytest.skip("需 MySQL 驱动环境")
        from insflow.core.entities import Workspace
        from insflow.core.store import Store

        s = Store()
        await s.connect()
        await s.migrate()
        await s.create_workspace(Workspace(id="vis-ws", name="v"))   # 写入并 commit
        await s._fetchone("SELECT COUNT(*) AS n FROM workspaces")     # 开出读快照

        import pymysql

        from insflow.core.db import mysql_config_from_env
        cfg = mysql_config_from_env()
        kwargs = {"user": cfg["user"], "password": cfg.get("password", ""),
                  "database": cfg["dbname"], "charset": "utf8mb4",
                  "autocommit": True}
        if cfg.get("socket"):
            kwargs["unix_socket"] = cfg["socket"]
        else:
            kwargs["host"] = cfg["host"]
            kwargs["port"] = int(cfg.get("port", 3306))
        ext = pymysql.connect(**kwargs)
        try:
            with ext.cursor() as cur:
                cur.execute(
                    "INSERT INTO workspaces (id, name, stage, maturity_level, "
                    "settings_json, created_at, updated_at) "
                    "VALUES ('vis-ext', 'external', 'S0', 'L0', '{}', "
                    "'2026-01-01T00:00:00', '2026-01-01T00:00:00')")
            row = await s._fetchone("SELECT COUNT(*) AS n FROM workspaces")
            assert int(row["n"]) >= 2, "外部写入对服务不可见（隔离级别未生效）"
        finally:
            with ext.cursor() as cur:
                cur.execute("DELETE FROM workspaces WHERE id = 'vis-ext'")
            ext.close()
            await s._execute("DELETE FROM workspaces WHERE id = 'vis-ws'")
            await s._db.commit()
            await s.close()
