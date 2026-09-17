"""Insight Flow 数据库后端（SQLite / MySQL 双驱动）

对齐 OpenFlow `EventStore` 的演进：**MySQL 为主（生产/多租户），SQLite 内嵌辅助
（个人/自托管零依赖）**。两者对上层暴露同一接口，`Store` 只面向接口编程——
"换底座不换楼"。

切换：环境变量 `INSFLOW_DB_DRIVER=sqlite|mysql` + `MYSQL_HOST/PORT/DBNAME/USER/PASS`
（命名对齐 OpenFlow settings.json 的 mysql_* 约定）。
"""

import os
from typing import Any

from .dialect import get_dialect

try:  # 可选依赖：仅 MySQL 驱动需要（用同步 pymysql + 线程池，避免异步驱动版本冲突）
    import pymysql  # type: ignore
    from pymysql.cursors import DictCursor  # type: ignore
except Exception:  # pragma: no cover
    pymysql = None
    DictCursor = None

SQLITE_PRAGMAS = [
    "PRAGMA journal_mode=WAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA foreign_keys=ON",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA temp_store=MEMORY",
    "PRAGMA cache_size=-20000",
    "PRAGMA mmap_size=268435456",
]

MYSQL_SESSION_SETTINGS = [
    "SET SESSION sql_mode='STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION'",
    "SET SESSION time_zone='+00:00'",
]


class Cursor:
    """统一游标（fetchone/fetchall/rowcount）——同时兼容 aiosqlite 与 pymysql"""

    def __init__(self, cur, driver: str):
        self._cur = cur
        self._driver = driver

    async def fetchone(self):
        row = await self._maybe_await(self._cur.fetchone())
        return row

    async def fetchall(self):
        rows = await self._maybe_await(self._cur.fetchall())
        return list(rows)

    async def _maybe_await(self, value):
        import inspect
        if inspect.isawaitable(value):
            return await value
        return value

    @property
    def rowcount(self) -> int:
        return getattr(self._cur, "rowcount", 0) or 0


class SQLiteBackend:
    """SQLite 后端（aiosqlite）：内嵌零依赖，单机/个人/自托管默认"""

    driver = "sqlite"

    def __init__(self, db_path):
        self.db_path = db_path
        self.dialect = get_dialect("sqlite")
        self._conn = None
        self.row_factory = None  # 由 Store 设置

    async def connect(self) -> None:
        import aiosqlite
        from pathlib import Path
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self.db_path))
        self._conn.row_factory = aiosqlite.Row
        for pragma in SQLITE_PRAGMAS:
            await self._conn.execute(pragma)

    async def execute(self, sql: str, params: tuple = ()) -> Cursor:
        cur = await self._conn.execute(self.dialect.adapt(sql), params)
        return Cursor(cur, "sqlite")

    async def executescript(self, script: str) -> None:
        await self._conn.executescript(script)

    async def commit(self) -> None:
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def wal_checkpoint(self, mode: str = "TRUNCATE") -> None:
        await self._conn.execute(f"PRAGMA wal_checkpoint({mode})")
        await self._conn.commit()

    async def count(self, table: str) -> int | None:
        try:
            cur = await self.execute(f"SELECT COUNT(*) AS n FROM {table}")
            row = await cur.fetchone()
            return int(row["n"]) if row else 0
        except Exception:
            return None

    async def size_bytes(self) -> tuple[int, int]:
        from pathlib import Path
        db = Path(self.db_path)
        size = db.stat().st_size if db.exists() else 0
        wal = Path(str(db) + "-wal")
        return size, (wal.stat().st_size if wal.exists() else 0)


class MySQLBackend:
    """MySQL 后端（pymysql + asyncio.to_thread）

    生产/多租户推荐（与 OpenFlow 事件表同选型）。选同步驱动 + 线程池的原因：
    1. 异步 MySQL 驱动（aiomysql）与 pymysql 版本耦合多、易踩兼容坑
    2. 驾驶舱聚合是"少而重"的查询，线程池足够；避免额外异步依赖
    3. 与 OpenFlow 的做法一致（PHP 侧也是同步 PDO）
    """

    driver = "mysql"

    def __init__(self, config: dict):
        self.config = config
        self.dialect = get_dialect("mysql")
        self._lib = None
        self._pool = None           # queue.Queue of connections
        self.row_factory = None

    @property
    def dsn(self) -> str:
        c = self.config
        return f"{c.get('user')}@{c.get('host')}:{c.get('port', 3306)}/{c.get('dbname')}"

    def _new_conn(self):
        c = self.config
        return self._lib.connect(
            host=c["host"], port=int(c.get("port", 3306)),
            user=c["user"], password=c.get("password", ""),
            database=c["dbname"], charset="utf8mb4",
            cursorclass=DictCursor, autocommit=False,
            connect_timeout=8, read_timeout=30,
        )

    async def connect(self) -> None:
        if pymysql is None:
            raise RuntimeError(
                "MySQL 驱动未安装：pip install pymysql（或 pip install -e '.[mysql]'）")
        self._lib = pymysql
        c = self.config
        if not (c.get("host") and c.get("dbname") and c.get("user")):
            raise RuntimeError("MySQL 配置不完整（MYSQL_HOST/MYSQL_DBNAME/MYSQL_USER）")

        import asyncio
        import queue
        size = max(1, int(c.get("pool_size", 5)))
        self._pool = queue.Queue(maxsize=size)
        # 先建一条以验证连通性（fail-closed：连不上直接报错，不静默回落）
        try:
            conn = await asyncio.to_thread(self._new_conn)
        except Exception as e:
            raise RuntimeError(f"MySQL 连接失败（{self.dsn}）：{e}") from e
        self._pool.put(conn)
        for _ in range(size - 1):
            self._pool.put(None)   # 懒建；取到 None 时创建
        await self.execute("SET SESSION time_zone='+00:00'")

    async def _run(self, fn):
        """在池中取连接执行（线程内）"""
        import asyncio
        import queue
        try:
            conn = self._pool.get_nowait()
        except queue.Empty:
            conn = await asyncio.to_thread(self._pool.get)
        if conn is None:
            conn = await asyncio.to_thread(self._new_conn)
        try:
            cur = conn.cursor()
            out = fn(cur)
            conn.commit()
            return out, cur
        finally:
            self._pool.put(conn)

    async def execute(self, sql: str, params: tuple = ()) -> Cursor:
        import asyncio
        adapted = self.dialect.adapt(sql)

        def _do(cur):
            cur.execute(adapted, params or None)
            return cur

        cur, _ = await self._run(_do)
        return Cursor(cur, "mysql")

    async def executescript(self, script: str) -> None:
        statements = [s.strip() for s in script.split(";") if s.strip()]
        for stmt in statements:
            await self.execute(stmt)

    async def commit(self) -> None:
        return None    # _run 每次已提交

    async def close(self) -> None:
        if self._pool:
            while not self._pool.empty():
                conn = self._pool.get()
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
            self._pool = None

    async def wal_checkpoint(self, mode: str = "TRUNCATE") -> None:
        return None    # MySQL 无 WAL

    async def count(self, table: str) -> int | None:
        try:
            cur = await self.execute(f"SELECT COUNT(*) AS n FROM {table}")
            row = await cur.fetchone()
            return int(row["n"]) if row else 0
        except Exception:
            return None

    async def size_bytes(self) -> tuple[int, int]:
        try:
            cur = await self.execute(
                """SELECT COALESCE(SUM(data_length + index_length), 0) AS n
                   FROM information_schema.tables WHERE table_schema = %s""",
                (self.config.get("dbname"),))
            row = await cur.fetchone()
            return (int(row["n"]) if row else 0), 0
        except Exception:
            return 0, 0


def mysql_config_from_env() -> dict:
    """从环境变量读取 MySQL 配置（命名对齐 OpenFlow 的 mysql_* 约定）"""
    return {
        "host": os.environ.get("MYSQL_HOST", "127.0.0.1"),
        "port": int(os.environ.get("MYSQL_PORT", "3306")),
        "dbname": os.environ.get("MYSQL_DBNAME", "insflow"),
        "user": os.environ.get("MYSQL_USER", "insflow"),
        "password": os.environ.get("MYSQL_PASS", ""),
        "pool_size": int(os.environ.get("MYSQL_POOL_SIZE", "5")),
    }


def resolve_driver(explicit: str | None = None) -> str:
    driver = (explicit or os.environ.get("INSFLOW_DB_DRIVER", "sqlite")).lower()
    return "mysql" if driver == "mysql" else "sqlite"


def create_backend(driver: str | None = None, db_path=None):
    """按驱动创建后端"""
    resolved = resolve_driver(driver)
    if resolved == "mysql":
        return MySQLBackend(mysql_config_from_env())
    from ..store import default_db_path
    return SQLiteBackend(db_path or default_db_path())
