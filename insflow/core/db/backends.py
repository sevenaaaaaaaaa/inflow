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

try:  # 可选依赖：仅 MySQL 驱动需要
    import aiomysql  # type: ignore
except Exception:  # pragma: no cover
    aiomysql = None

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
    """统一游标（fetchone/fetchall/rowcount）"""

    def __init__(self, cur, driver: str):
        self._cur = cur
        self._driver = driver

    async def fetchone(self):
        row = await self._cur.fetchone() if self._driver == "mysql" else await self._cur.fetchone()
        if row is None:
            return None
        if self._driver == "mysql":
            return row  # aiomysql 返回 dict（DictCursor）
        return row

    async def fetchall(self):
        rows = await self._cur.fetchall()
        return list(rows)

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
    """MySQL 后端（aiomysql）：生产/多租户推荐（与 OpenFlow 事件表同选型）"""

    driver = "mysql"

    def __init__(self, config: dict):
        self.config = config
        self.dialect = get_dialect("mysql")
        self._pool = None
        self._conn = None
        self.row_factory = None

    @property
    def dsn(self) -> str:
        c = self.config
        return f"{c.get('user')}@{c.get('host')}:{c.get('port', 3306)}/{c.get('dbname')}"

    async def connect(self) -> None:
        if aiomysql is None:
            raise RuntimeError(
                "MySQL 驱动未安装：pip install aiomysql（或 pip install -e '.[mysql]'）")
        c = self.config
        if not (c.get("host") and c.get("dbname") and c.get("user")):
            raise RuntimeError("MySQL 配置不完整（MYSQL_HOST/MYSQL_DBNAME/MYSQL_USER）")
        self._pool = await aiomysql.create_pool(
            host=c["host"], port=int(c.get("port", 3306)),
            user=c["user"], password=c.get("password", ""),
            db=c["dbname"], charset="utf8mb4", autocommit=False,
            minsize=1, maxsize=int(c.get("pool_size", 5)),
        )
        self._conn = await self._pool.acquire()
        for setting in MYSQL_SESSION_SETTINGS:
            async with self._conn.cursor() as cur:
                await cur.execute(setting)

    async def execute(self, sql: str, params: tuple = ()) -> Cursor:
        cur = await self._conn.cursor(aiomysql.DictCursor)
        await cur.execute(self.dialect.adapt(sql), params or None)
        return Cursor(cur, "mysql")

    async def executescript(self, script: str) -> None:
        """按 ; 拆分执行（MySQL 驱动不支持多语句 exec 的通用写法）"""
        statements = [s.strip() for s in script.split(";") if s.strip()]
        async with self._conn.cursor() as cur:
            for stmt in statements:
                await cur.execute(stmt)

    async def commit(self) -> None:
        await self._conn.commit()

    async def close(self) -> None:
        if self._pool:
            if self._conn:
                self._pool.release(self._conn)
                self._conn = None
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None

    async def wal_checkpoint(self, mode: str = "TRUNCATE") -> None:
        return None  # MySQL 无 WAL

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
            return int(row["n"]) if row else 0, 0
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
