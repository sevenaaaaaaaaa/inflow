"""Insight Flow SQLite 存储层"""

import json
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from .entities import (
    Action,
    Insight,
    Workspace,
)

# 默认数据库路径
DEFAULT_DB_PATH = Path(__file__).parent.parent.parent / "data" / "insflow.db"

# SQL 迁移脚本
MIGRATIONS = [
    # V1: 核心表
    """
    CREATE TABLE IF NOT EXISTS workspaces (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        stage TEXT NOT NULL DEFAULT 'S0',
        maturity_level TEXT NOT NULL DEFAULT 'L0',
        settings_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS sources (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES workspaces(id),
        plugin_id TEXT NOT NULL,
        config_enc TEXT NOT NULL DEFAULT '{}',
        quota_ledger_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS monitors (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES workspaces(id),
        kind TEXT NOT NULL,
        target_json TEXT NOT NULL DEFAULT '{}',
        schedule_cron TEXT NOT NULL DEFAULT '0 */6 * * *',
        last_run_at TEXT,
        state TEXT NOT NULL DEFAULT 'idle',
        created_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS raw_records (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES workspaces(id),
        source_id TEXT NOT NULL REFERENCES sources(id),
        monitor_id TEXT NOT NULL REFERENCES monitors(id),
        kind TEXT NOT NULL,
        payload_json TEXT NOT NULL DEFAULT '{}',
        captured_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS metrics (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES workspaces(id),
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        metric TEXT NOT NULL,
        value REAL NOT NULL,
        dim_json TEXT NOT NULL DEFAULT '{}',
        ts TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS insights (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES workspaces(id),
        type TEXT NOT NULL,
        title TEXT NOT NULL,
        summary TEXT NOT NULL,
        severity TEXT NOT NULL DEFAULT 'medium',
        confidence REAL NOT NULL DEFAULT 0.5,
        evidence_json TEXT NOT NULL DEFAULT '[]',
        models_json TEXT NOT NULL DEFAULT '[]',
        actions_json TEXT NOT NULL DEFAULT '[]',
        stage_tags_json TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'new',
        created_at TEXT NOT NULL,
        verified_at TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS actions (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES workspaces(id),
        insight_id TEXT NOT NULL REFERENCES insights(id),
        action_type TEXT NOT NULL,
        target_ref TEXT,
        params_json TEXT NOT NULL DEFAULT '{}',
        state TEXT NOT NULL DEFAULT 'pending',
        dispatched_at TEXT,
        result_json TEXT NOT NULL DEFAULT '{}',
        verify_window_until TEXT,
        baseline_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS feedback (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES workspaces(id),
        action_id TEXT NOT NULL REFERENCES actions(id),
        metric TEXT NOT NULL,
        before REAL NOT NULL,
        after REAL NOT NULL,
        delta REAL NOT NULL,
        verdict TEXT NOT NULL,
        evaluated_at TEXT NOT NULL
    );
    """,
    # 索引
    """
    CREATE INDEX IF NOT EXISTS idx_sources_workspace ON sources(workspace_id);
    CREATE INDEX IF NOT EXISTS idx_monitors_workspace ON monitors(workspace_id);
    CREATE INDEX IF NOT EXISTS idx_raw_records_workspace ON raw_records(workspace_id);
    CREATE INDEX IF NOT EXISTS idx_raw_records_source ON raw_records(source_id);
    CREATE INDEX IF NOT EXISTS idx_metrics_entity ON metrics(entity_type, entity_id);
    CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metrics(ts);
    CREATE INDEX IF NOT EXISTS idx_insights_workspace ON insights(workspace_id);
    CREATE INDEX IF NOT EXISTS idx_insights_status ON insights(status);
    CREATE INDEX IF NOT EXISTS idx_insights_severity ON insights(severity);
    CREATE INDEX IF NOT EXISTS idx_actions_workspace ON actions(workspace_id);
    CREATE INDEX IF NOT EXISTS idx_actions_insight ON actions(insight_id);
    CREATE INDEX IF NOT EXISTS idx_actions_state ON actions(state);
    """,
]


def generate_id() -> str:
    """生成唯一ID"""
    import uuid
    return str(uuid.uuid4())[:8]


def to_json(value) -> str:
    """JSON 序列化（兼容 pydantic 模型嵌套）"""
    def _default(o):
        if hasattr(o, "model_dump"):
            return o.model_dump(mode="json")
        return str(o)
    return json.dumps(value, ensure_ascii=False, default=_default)


class Store:
    """SQLite 存储层"""

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self._db: aiosqlite.Connection | None = None

    async def connect(self):
        """连接数据库"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self.db_path))
        self._db.row_factory = aiosqlite.Row
        # 启用 WAL 模式
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")

    async def close(self):
        """关闭连接"""
        if self._db:
            await self._db.close()
            self._db = None

    async def migrate(self):
        """执行迁移"""
        if not self._db:
            raise RuntimeError("Database not connected")
        for migration in MIGRATIONS:
            await self._db.executescript(migration)
        await self._db.commit()

    async def _execute(self, query: str, params: tuple = ()) -> aiosqlite.Cursor:
        """执行查询"""
        if not self._db:
            raise RuntimeError("Database not connected")
        return await self._db.execute(query, params)

    async def _fetchone(self, query: str, params: tuple = ()) -> dict | None:
        """获取单条记录"""
        cursor = await self._execute(query, params)
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def _fetchall(self, query: str, params: tuple = ()) -> list[dict]:
        """获取多条记录"""
        cursor = await self._execute(query, params)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # ========== Workspace ==========

    async def create_workspace(self, ws: Workspace) -> Workspace:
        """创建工作区"""
        ws.id = ws.id or generate_id()
        now = datetime.now(UTC)
        ws.created_at = now
        ws.updated_at = now
        await self._execute(
            """INSERT INTO workspaces (id, name, stage, maturity_level, settings_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (ws.id, ws.name, ws.stage.value, ws.maturity_level.value,
             to_json(ws.settings_json), ws.created_at.isoformat(), ws.updated_at.isoformat())
        )
        await self._db.commit()
        return ws

    async def get_workspace(self, workspace_id: str) -> Workspace | None:
        """获取工作区"""
        row = await self._fetchone("SELECT * FROM workspaces WHERE id = ?", (workspace_id,))
        if row:
            row["settings_json"] = json.loads(row["settings_json"]) if row["settings_json"] else {}
            return Workspace(**row)
        return None

    async def list_workspaces(self) -> list[Workspace]:
        """列出所有工作区"""
        rows = await self._fetchall("SELECT * FROM workspaces ORDER BY created_at DESC")
        result = []
        for row in rows:
            row["settings_json"] = json.loads(row["settings_json"]) if row["settings_json"] else {}
            result.append(Workspace(**row))
        return result

    async def update_workspace(self, ws: Workspace) -> Workspace:
        """更新工作区（成熟度引擎写回 stage/maturity_level 等）"""
        ws.updated_at = datetime.now(UTC)
        await self._execute(
            """UPDATE workspaces
               SET name = ?, stage = ?, maturity_level = ?, settings_json = ?, updated_at = ?
               WHERE id = ?""",
            (ws.name, ws.stage.value, ws.maturity_level.value,
             to_json(ws.settings_json), ws.updated_at.isoformat(), ws.id)
        )
        await self._db.commit()
        return ws

    # ========== Insight ==========

    async def create_insight(self, insight: Insight) -> Insight:
        """创建洞察"""
        insight.id = insight.id or generate_id()
        insight.created_at = datetime.now(UTC)
        await self._execute(
            """INSERT INTO insights (id, workspace_id, type, title, summary, severity, confidence,
               evidence_json, models_json, actions_json, stage_tags_json, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (insight.id, insight.workspace_id, insight.type, insight.title, insight.summary,
             insight.severity.value, insight.confidence, to_json(insight.evidence_json),
             to_json(insight.models_json), to_json(insight.actions_json), to_json(insight.stage_tags_json),
             insight.status.value, insight.created_at.isoformat())
        )
        await self._db.commit()
        return insight

    def _parse_insight_row(self, row: dict) -> Insight:
        """解析洞察行数据"""
        for field in ["evidence_json", "models_json", "stage_tags_json"]:
            row[field] = json.loads(row[field]) if row[field] else []
        row["actions_json"] = json.loads(row["actions_json"]) if row["actions_json"] else []
        return Insight(**row)

    async def get_insight(self, insight_id: str) -> Insight | None:
        """获取洞察"""
        row = await self._fetchone("SELECT * FROM insights WHERE id = ?", (insight_id,))
        if row:
            return self._parse_insight_row(row)
        return None

    async def list_insights(
        self,
        workspace_id: str,
        status: str | None = None,
        severity: str | None = None,
        limit: int = 50
    ) -> list[Insight]:
        """列出洞察"""
        query = "SELECT * FROM insights WHERE workspace_id = ?"
        params = [workspace_id]
        if status:
            query += " AND status = ?"
            params.append(status)
        if severity:
            query += " AND severity = ?"
            params.append(severity)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = await self._fetchall(query, tuple(params))
        return [self._parse_insight_row(row) for row in rows]

    async def update_insight_status(self, insight_id: str, status: str) -> bool:
        """更新洞察状态"""
        await self._execute(
            "UPDATE insights SET status = ? WHERE id = ?",
            (status, insight_id)
        )
        await self._db.commit()
        return True

    # ========== Action ==========

    async def create_action(self, action: Action) -> Action:
        """创建动作"""
        action.id = action.id or generate_id()
        action.created_at = datetime.now(UTC)
        await self._execute(
            """INSERT INTO actions (id, workspace_id, insight_id, action_type, target_ref,
               params_json, state, dispatched_at, result_json, verify_window_until, baseline_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (action.id, action.workspace_id, action.insight_id, action.action_type,
             action.target_ref, to_json(action.params_json), action.state.value,
             action.dispatched_at.isoformat() if action.dispatched_at else None,
             to_json(action.result_json),
             action.verify_window_until.isoformat() if action.verify_window_until else None,
             to_json(action.baseline_json), action.created_at.isoformat())
        )
        await self._db.commit()
        return action

    async def list_actions(self, workspace_id: str, insight_id: str | None = None) -> list[Action]:
        """列出动作"""
        query = "SELECT * FROM actions WHERE workspace_id = ?"
        params = [workspace_id]
        if insight_id:
            query += " AND insight_id = ?"
            params.append(insight_id)
        query += " ORDER BY created_at DESC"
        rows = await self._fetchall(query, tuple(params))
        return [Action(**row) for row in rows]


# 全局存储实例
_store: Store | None = None


async def get_store() -> Store:
    """获取全局存储实例"""
    global _store
    if _store is None:
        _store = Store()
        await _store.connect()
        await _store.migrate()
    return _store


async def close_store() -> None:
    """关闭全局存储实例（CLI 一次性命令结束时调用，避免 aiosqlite 线程阻塞退出）"""
    global _store
    if _store is not None:
        await _store.close()
        _store = None


def reset_store(store: Store | None) -> None:
    """替换全局 store（测试隔离用）"""
    global _store
    _store = store
