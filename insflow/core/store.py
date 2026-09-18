"""Insight Flow SQLite 存储层"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from .entities import (
    Action,
    Feedback,
    Insight,
    Workspace,
)

# 默认数据库路径
DEFAULT_DB_PATH = Path(__file__).parent.parent.parent / "data" / "insflow.db"


def default_db_path() -> Path:
    """运行时推导 db 路径

    优先级：INSFLOW_DB_PATH（显式指定，迁移/多盘部署的切换点）
            > DATA_DIR/insflow.db（默认）
    说明（对齐 OpenFlow ADR-2）：SQLite 起步，预留 DATABASE_URL 切换点；
    当触发器命中（见 docs/11）时，改为通过 Store 的查询层实现替换为 MySQL/Postgres。
    """
    explicit = os.environ.get("INSFLOW_DB_PATH", "")
    if explicit:
        return Path(explicit)
    from . import files as files_mod
    base = files_mod.DATA_DIR or DEFAULT_DB_PATH.parent
    return Path(base) / "insflow.db"

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
        `before` REAL NOT NULL,
        `after` REAL NOT NULL,
        delta REAL NOT NULL,
        verdict TEXT NOT NULL,
        evaluated_at TEXT NOT NULL
    );
    """,
    # V2: 竞品档案 + 关键词排名追踪 + 旅程事件
    """
    CREATE TABLE IF NOT EXISTS competitors (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES workspaces(id),
        domain TEXT NOT NULL,
        name TEXT NOT NULL DEFAULT '',
        positioning TEXT NOT NULL DEFAULT '',
        pricing_json TEXT NOT NULL DEFAULT '[]',
        product_lines_json TEXT NOT NULL DEFAULT '[]',
        social_json TEXT NOT NULL DEFAULT '{}',
        monitors_json TEXT NOT NULL DEFAULT '[]',
        seo_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL,
        UNIQUE(workspace_id, domain)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS keyword_ranks (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES workspaces(id),
        competitor_domain TEXT NOT NULL,
        keyword TEXT NOT NULL,
        position INTEGER,
        volume INTEGER,
        url TEXT NOT NULL DEFAULT '',
        is_mine INTEGER NOT NULL DEFAULT 0,
        checked_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS journey_events (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES workspaces(id),
        identity TEXT NOT NULL,
        stage TEXT NOT NULL,
        event TEXT NOT NULL,
        props_json TEXT NOT NULL DEFAULT '{}',
        source TEXT NOT NULL DEFAULT 'openflow',
        ts TEXT NOT NULL
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
    CREATE INDEX IF NOT EXISTS idx_competitors_workspace ON competitors(workspace_id);
    CREATE INDEX IF NOT EXISTS idx_keyword_ranks_ws ON keyword_ranks(workspace_id, competitor_domain);
    CREATE INDEX IF NOT EXISTS idx_journey_identity ON journey_events(workspace_id, identity);
    CREATE INDEX IF NOT EXISTS idx_journey_stage ON journey_events(workspace_id, stage);
    """,
    # V4: 多租户自助（R4）——用户账号 + 会话
    """
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        email TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        name TEXT NOT NULL DEFAULT '',
        role TEXT NOT NULL DEFAULT 'owner',
        workspace_id TEXT,
        external_id TEXT NOT NULL DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
    """,
    # V5: 洞察订阅（G-5）——规则 + 渠道 + 过滤
    """
    CREATE TABLE IF NOT EXISTS subscriptions (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        name TEXT NOT NULL,
        channels_json TEXT NOT NULL DEFAULT '[]',
        target_json TEXT NOT NULL DEFAULT '{}',
        filters_json TEXT NOT NULL DEFAULT '{}',
        mode TEXT NOT NULL DEFAULT 'immediate',
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_subscriptions_ws ON subscriptions(workspace_id);
    """,
    # V6: 指标分析索引（驾驶舱聚合：避免全表扫描）
    """
    CREATE INDEX IF NOT EXISTS idx_metrics_ws_metric_ts
        ON metrics(workspace_id, metric, ts);
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_metrics_ws_entity_metric_ts
        ON metrics(workspace_id, entity_id, metric, ts);
    """,

    # V4: 预聚合（物化视图等价物）+ 语义层 + 协作 + 告警
    """
    CREATE TABLE IF NOT EXISTS metric_daily (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        metric TEXT NOT NULL,
        day TEXT NOT NULL,
        entity_type TEXT NOT NULL DEFAULT 'site',
        entity_id TEXT NOT NULL DEFAULT 'main',
        dim_key TEXT NOT NULL DEFAULT '',
        agg_sum REAL NOT NULL DEFAULT 0,
        agg_avg REAL NOT NULL DEFAULT 0,
        agg_max REAL NOT NULL DEFAULT 0,
        n INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    );
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_metric_daily_key
    ON metric_daily(workspace_id, metric, day, entity_type, entity_id, dim_key);
    """,
    """
    CREATE TABLE IF NOT EXISTS metric_defs (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        name TEXT NOT NULL,
        label TEXT NOT NULL DEFAULT '',
        expr TEXT NOT NULL DEFAULT '',
        unit TEXT NOT NULL DEFAULT '',
        owner TEXT NOT NULL DEFAULT '',
        version INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'active',
        notes TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS comments (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        target_type TEXT NOT NULL,
        target_id TEXT NOT NULL,
        author TEXT NOT NULL DEFAULT '',
        body TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS alert_rules (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        name TEXT NOT NULL,
        metric TEXT NOT NULL,
        op TEXT NOT NULL DEFAULT 'gt',
        threshold REAL NOT NULL DEFAULT 0,
        window_days REAL NOT NULL DEFAULT 7,
        dims_json TEXT NOT NULL DEFAULT '{}',
        routes_json TEXT NOT NULL DEFAULT '[]',
        escalation_json TEXT NOT NULL DEFAULT '{}',
        enabled INTEGER NOT NULL DEFAULT 1,
        last_fired_at TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS admin_audit (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        actor TEXT NOT NULL DEFAULT '',
        role TEXT NOT NULL DEFAULT '',
        action TEXT NOT NULL,
        target_type TEXT NOT NULL DEFAULT '',
        target_id TEXT NOT NULL DEFAULT '',
        detail_json TEXT NOT NULL DEFAULT '{}',
        ip TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_admin_audit_ws_time
        ON admin_audit(workspace_id, created_at);
    """,

]

# V5: users 增加 SCIM 字段（external_id / active）——SQLite ALTER 幂等
V5_USERS_COLUMNS = [
    "ALTER TABLE users ADD COLUMN external_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE users ADD COLUMN active INTEGER NOT NULL DEFAULT 1",
]


# V3: metrics 幂等去重（R1-4）—— ALTER 语句需要幂等执行（检查列是否存在）
V3_METRICS_DEDUPE_SQL = [
    "ALTER TABLE metrics ADD COLUMN monitor_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE metrics ADD COLUMN window_key TEXT NOT NULL DEFAULT ''",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_metrics_dedupe
       ON metrics(workspace_id, monitor_id, entity_type, entity_id, metric, window_key)
       WHERE window_key != ''""",
]


def generate_id() -> str:
    """生成唯一ID

    注意：曾用 `uuid4()[:8]`（16^8 ≈ 4.3e9 空间），按生日悖论约 7.7 万行即 ~50%
    概率碰撞（实测 20 万行批量插入直接 UNIQUE 冲突）。改为 16 位十六进制（2^64）。
    """
    import uuid
    return uuid.uuid4().hex[:16]
new_id = generate_id  # 别名：新增行主键



def to_json(value) -> str:
    """JSON 序列化（兼容 pydantic 模型嵌套）"""
    def _default(o):
        if hasattr(o, "model_dump"):
            return o.model_dump(mode="json")
        return str(o)
    return json.dumps(value, ensure_ascii=False, default=_default)


def dim_window_key(ts_iso: str, dim: dict | None = None) -> str:
    """幂等窗口键 = 数据时间（小时） + 维度指纹

    唯一索引是 (workspace, monitor, entity_type, entity_id, metric, window_key)，
    不含 dim_json。带维度的指标（地域/渠道/设备）若共用同一窗口键会互相覆盖
    （SQLite 静默忽略、MySQL 唯一键报错），因此维度非空时附加 6 位指纹。
    """
    key = str(ts_iso)[:13].replace("T", "-").replace(":", "")
    if dim:
        import hashlib as _hashlib
        fp = _hashlib.sha1(
            json.dumps(dim, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:6]
        return f"{key}#{fp}"
    return key


class Store:
    """SQLite 存储层"""

    SLOW_QUERY_MS = 200.0          # 慢查询阈值（超过则记录，供运维舱观察）
    _DURATION_SAMPLES = 500        # 保留最近 N 次耗时用于 p95

    def __init__(self, db_path: Path | None = None, driver: str | None = None):
        self.db_path = db_path or default_db_path()
        self.driver = (driver or os.environ.get("INSFLOW_DB_DRIVER", "sqlite")).lower()
        self._db = None          # 后端实例（SQLiteBackend / MySQLBackend，接口统一）
        self._backend = None
        self._query_count = 0
        self._slow_queries = 0
        self._query_durations: list[float] = []

    async def connect(self):
        """连接数据库（SQLite / MySQL 双驱动，对齐 OpenFlow EventStore）"""
        from .db import create_backend
        self._backend = create_backend(driver=self.driver, db_path=self.db_path)
        await self._backend.connect()
        self._db = self._backend          # 统一接口别名（execute/commit/executescript/close）
        self.driver = self._backend.driver

    async def wal_checkpoint(self, mode: str = "TRUNCATE") -> None:
        """WAL 检查点（仅 SQLite；MySQL 无 WAL）"""
        if self._backend:
            await self._backend.wal_checkpoint(mode)

    async def health(self) -> dict:
        """数据库健康与增长指标（对齐 OpenFlow：看"摊了多少读/耗时"，不只看表大小）

        返回：文件大小 / WAL 大小 / 各表行数 / 慢查询统计 / 查询计数
        """
        size, wal_size = await self._backend.size_bytes()

        tables = ["insights", "metrics", "actions", "feedback", "events",
                  "raw_records", "journey_events", "subscriptions", "monitors",
                  "competitors", "users", "sessions"]
        row_counts = {}
        for t in tables:
            row_counts[t] = await self._backend.count(t)

        counts = sorted(self._query_durations)
        p95 = counts[int(len(counts) * 0.95)] if counts else 0.0
        return {
            "driver": self.driver,
            "db_size_bytes": size,
            "wal_size_bytes": wal_size,
            "row_counts": row_counts,
            "queries": self._query_count,
            "slow_queries": self._slow_queries,
            "slow_threshold_ms": int(self.SLOW_QUERY_MS),
            "p95_ms": round(p95, 2),
            "max_ms": round(max(counts), 2) if counts else 0.0,
        }

    async def close(self):
        """关闭连接"""
        if self._db:
            await self._db.close()
            self._db = None

    async def migrate(self):
        """执行迁移"""
        if not self._db:
            raise RuntimeError("Database not connected")
        if self.driver == "mysql":
            from .db.schema_mysql import MYSQL_MIGRATIONS
            for migration in MYSQL_MIGRATIONS:
                try:
                    await self._backend.executescript(migration)
                except Exception as e:  # 已存在等幂等错误容忍（MySQL 无 IF NOT EXISTS 索引）
                    if "Duplicate" not in str(e) and "already exists" not in str(e):
                        raise
            await self._db.commit()
            return
        for migration in MIGRATIONS:
            await self._db.executescript(migration)
        # V3 幂等迁移：metrics 去重列/索引（按列存在性跳过 ALTER）
        cols = {row[1] for row in await (await self._execute("PRAGMA table_info(metrics)")).fetchall()}
        for statement in V3_METRICS_DEDUPE_SQL:
            if statement.startswith("ALTER TABLE"):
                col_name = statement.split("ADD COLUMN")[1].strip().split(" ")[0]
                if col_name in cols:
                    continue
            await self._execute(statement)
        # V5：users 的 SCIM 字段（按列存在性跳过）
        ucols = {row[1] for row in
                 await (await self._execute("PRAGMA table_info(users)")).fetchall()}
        for statement in V5_USERS_COLUMNS:
            col_name = statement.split("ADD COLUMN")[1].strip().split(" ")[0]
            if col_name in ucols:
                continue
            await self._execute(statement)
        await self._db.commit()

    def _record_query(self, elapsed_ms: float, query: str) -> None:
        """查询计时（性能观测：慢查询 + p95）"""
        self._query_count += 1
        samples = self._query_durations
        samples.append(elapsed_ms)
        if len(samples) > self._DURATION_SAMPLES:
            del samples[0]
        if elapsed_ms >= self.SLOW_QUERY_MS:
            self._slow_queries += 1
            import logging
            logging.getLogger("insflow.db").warning(
                "slow query %.1fms: %s", elapsed_ms, " ".join(query.split())[:120])

    async def _execute(self, query: str, params: tuple = ()) -> aiosqlite.Cursor:
        """执行查询（带计时）"""
        if not self._db:
            raise RuntimeError("Database not connected")
        import time as _t
        t0 = _t.perf_counter()
        try:
            return await self._db.execute(query, params)
        finally:
            self._record_query((_t.perf_counter() - t0) * 1000, query)

    async def _fetchone(self, query: str, params: tuple = ()) -> dict | None:
        """获取单条记录（含取数耗时）"""
        import time as _t
        cursor = await self._execute(query, params)
        t0 = _t.perf_counter()
        row = await cursor.fetchone()
        self._record_query((_t.perf_counter() - t0) * 1000, query)
        return dict(row) if row else None

    async def _fetchall(self, query: str, params: tuple = ()) -> list[dict]:
        """获取多条记录（含取数耗时）"""
        import time as _t
        cursor = await self._execute(query, params)
        t0 = _t.perf_counter()
        rows = await cursor.fetchall()
        self._record_query((_t.perf_counter() - t0) * 1000, query)
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

    async def count_insights(self, workspace_id: str,
                             status: str | None = None,
                             severity: str | None = None) -> int:
        """洞察总数（分页用）"""
        query = "SELECT COUNT(*) as cnt FROM insights WHERE workspace_id = ?"
        params = [workspace_id]
        if status:
            query += " AND status = ?"
            params.append(status)
        if severity:
            query += " AND severity = ?"
            params.append(severity)
        row = await self._fetchone(query, tuple(params))
        return row["cnt"] if row else 0

    async def list_insights(
        self,
        workspace_id: str,
        status: str | None = None,
        severity: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Insight]:
        """列出洞察（offset 分页）"""
        query = "SELECT * FROM insights WHERE workspace_id = ?"
        params = [workspace_id]
        if status:
            query += " AND status = ?"
            params.append(status)
        if severity:
            query += " AND severity = ?"
            params.append(severity)
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
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
        return [self._parse_action_row(row) for row in rows]

    def _parse_action_row(self, row: dict) -> Action:
        """解析动作行数据（JSON 字段反序列化）"""
        for field in ["params_json", "result_json", "baseline_json"]:
            row[field] = json.loads(row[field]) if row[field] else {}
        return Action(**row)

    async def get_action(self, action_id: str) -> Action | None:
        """获取动作"""
        row = await self._fetchone("SELECT * FROM actions WHERE id = ?", (action_id,))
        if row:
            return self._parse_action_row(row)
        return None

    async def update_action_state(
        self,
        action_id: str,
        state: str,
        result_json: dict | None = None,
        dispatched_at: datetime | None = None,
        verify_window_until: datetime | None = None,
        baseline_json: dict | None = None,
    ) -> bool:
        """更新动作状态（配合状态机使用）"""
        fields = ["state = ?"]
        params: list = [state]
        if result_json is not None:
            fields.append("result_json = ?")
            params.append(to_json(result_json))
        if dispatched_at is not None:
            fields.append("dispatched_at = ?")
            params.append(dispatched_at.isoformat())
        if verify_window_until is not None:
            fields.append("verify_window_until = ?")
            params.append(verify_window_until.isoformat())
        if baseline_json is not None:
            fields.append("baseline_json = ?")
            params.append(to_json(baseline_json))
        params.append(action_id)
        await self._execute(
            f"UPDATE actions SET {', '.join(fields)} WHERE id = ?", tuple(params)
        )
        await self._db.commit()
        return True

    async def list_actions_due_for_verification(self, now: datetime | None = None) -> list[Action]:
        """列出验证窗口已到期、待评估的动作"""
        now = now or datetime.now(UTC)
        rows = await self._fetchall(
            """SELECT * FROM actions
               WHERE state IN ('verifying', 'done')
                 AND verify_window_until IS NOT NULL
                 AND verify_window_until <= ?""",
            (now.isoformat(),)
        )
        return [self._parse_action_row(row) for row in rows]

    # ========== Feedback ==========

    async def create_feedback(self, fb: Feedback) -> Feedback:
        """创建反馈"""
        fb.id = fb.id or generate_id()
        if fb.evaluated_at is None:
            fb.evaluated_at = datetime.now(UTC)
        await self._execute(
            """INSERT INTO feedback (id, workspace_id, action_id, metric, `before`, `after`, delta, verdict, evaluated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (fb.id, fb.workspace_id, fb.action_id, fb.metric, fb.before, fb.after,
             fb.delta, fb.verdict.value, fb.evaluated_at.isoformat())
        )
        await self._db.commit()
        return fb

    async def list_feedback(self, workspace_id: str, action_id: str | None = None) -> list[Feedback]:
        """列出反馈"""
        query = "SELECT * FROM feedback WHERE workspace_id = ?"
        params = [workspace_id]
        if action_id:
            query += " AND action_id = ?"
            params.append(action_id)
        query += " ORDER BY evaluated_at DESC"
        rows = await self._fetchall(query, tuple(params))
        return [Feedback(**row) for row in rows]

    async def get_feedback_stats(self, workspace_id: str) -> dict:
        """模型效果统计（北极星指标）"""
        rows = await self._fetchall(
            """SELECT verdict, COUNT(*) as cnt FROM feedback
               WHERE workspace_id = ? GROUP BY verdict""",
            (workspace_id,)
        )
        return {row["verdict"]: row["cnt"] for row in rows}

    async def get_model_effectiveness(self, workspace_id: str) -> list[dict]:
        """模型效果追踪（IM-5）：按模型聚合洞察命中率

        链路：insight.models_json → action(insight_id) → feedback.verdict
        命中率 = effective / (effective + neutral + harmful)
        """
        rows = await self._fetchall(
            """SELECT i.models_json, f.verdict, COUNT(*) as cnt
               FROM insights i
               JOIN actions a ON a.insight_id = i.id
               JOIN feedback f ON f.action_id = a.id
               WHERE i.workspace_id = ?
               GROUP BY i.models_json, f.verdict""",
            (workspace_id,)
        )
        stats: dict[str, dict[str, int]] = {}
        for row in rows:
            try:
                model_ids = json.loads(row["models_json"]) if row["models_json"] else []
            except (json.JSONDecodeError, TypeError):
                model_ids = []
            for mid in model_ids:
                entry = stats.setdefault(mid, {"effective": 0, "neutral": 0, "harmful": 0})
                if row["verdict"] in entry:
                    entry[row["verdict"]] += row["cnt"]

        result = []
        for mid, counts in stats.items():
            total = counts["effective"] + counts["neutral"] + counts["harmful"]
            hit_rate = counts["effective"] / total if total else 0.0
            result.append({
                "model_id": mid,
                "effective": counts["effective"],
                "neutral": counts["neutral"],
                "harmful": counts["harmful"],
                "hit_rate": round(hit_rate, 4),
            })
        result.sort(key=lambda x: -x["hit_rate"])
        return result

    # ========== 指标分析聚合（驾驶舱专用：降采样 + 限额）==========
    # 性能守则：一律走 (workspace_id, metric, ts) 索引；长窗口按天/周降采样；
    # 结果行数上限约束，避免把原始时序全量读进内存。

    MAX_SERIES_POINTS = 120

    @staticmethod
    def _bucket_fmt(days: float) -> str:
        if days <= 2:
            return "%Y-%m-%d %H"
        if days <= 90:
            return "%Y-%m-%d"
        return "%Y-W%W"

    def _dim_where(self, dim_filters: dict | None) -> tuple[str, list]:
        """维度等值过滤（cross-filter）：{province: 广东} → JSON 字段条件

        维度值是用户可控输入（URL 参数），因此只允许白名单键 + 参数化取值，
        并在取值为空时忽略该条件。
        """
        sql, params = "", []
        if not dim_filters:
            return sql, params
        allowed = {"province", "country", "channel", "device", "step_name",
                   "keyword", "query", "source", "campaign", "competitor"}
        for key, val in list(dim_filters.items())[:6]:
            if key not in allowed or val in (None, ""):
                continue
            expr = (self._backend.dialect.json_field("dim_json", key)
                    if self._backend else f"json_extract(dim_json, '$.{key}')")
            sql += f" AND {expr} = ?"
            params.append(str(val)[:80])
        return sql, params

    async def metric_series(self, workspace_id: str, metric: str,
                            days: float = 7, agg: str = "sum",
                            entity_id: str | None = None,
                            limit: int | None = None,
                            dim_filters: dict | None = None,
                            entity_allow: list[str] | None = None,
                            policy: tuple[str, list] | None = None) -> list[dict]:
        """按时间桶降采样的指标序列（驾驶舱趋势图）

        agg: sum | avg | max
        行数上限 MAX_SERIES_POINTS（超出按更大桶自动降采样）
        """
        from datetime import timedelta
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        fmt = self._bucket_fmt(days)
        bucket_expr = self._backend.dialect.bucket("ts", fmt) if self._backend else f"strftime('{fmt}', ts)"
        fn = {"sum": "SUM", "avg": "AVG", "max": "MAX"}.get(agg, "SUM")
        where = "workspace_id = ? AND metric = ? AND ts >= ?"
        params: list = [workspace_id, metric, since]
        if entity_id:
            where += " AND entity_id = ?"
            params.append(entity_id)
        if entity_allow:
            marks = ", ".join(["?"] * len(entity_allow[:50]))
            where += f" AND entity_id IN ({marks})"
            params.extend([str(v)[:80] for v in entity_allow[:50]])
        if policy and policy[0]:
            where += policy[0]
            params.extend(policy[1])
        # 长窗口优先读日汇总表（预聚合）：明细滚雪球后显著更快；无汇总自动回退
        # 注意：汇总表不承载 RLS policy / 实体白名单，这两类场景必须走明细，
        # 否则权限会被"快路径"绕过（安全 > 性能）。
        if days >= 30 and not entity_id and not policy and not entity_allow:
            try:
                from .rollup import daily_series
                rolled = await daily_series(workspace_id, metric, days=days, agg=agg,
                                            dim_filters=dim_filters)
                if rolled:
                    return rolled[:limit] if limit else rolled
            except Exception:
                pass
        dim_sql, dim_params = self._dim_where(dim_filters)
        where += dim_sql
        params.extend(dim_params)
        rows = await self._fetchall(
            f"""SELECT {bucket_expr} AS bucket, {fn}(value) AS v, COUNT(*) AS n
                FROM metrics WHERE {where}
                GROUP BY bucket ORDER BY bucket LIMIT ?""",
            tuple([*params, limit or self.MAX_SERIES_POINTS]),
        )
        return [{"bucket": r["bucket"], "value": float(r["v"] or 0), "n": r["n"]}
                for r in rows]

    async def metric_ohlc(self, workspace_id: str, metric: str, days: float = 90,
                          bucket: str = "day", entity_id: str | None = None,
                          dim_filters: dict | None = None,
                          limit: int = 20000) -> list[dict]:
        """真实 OHLC：按时间桶取桶内「首/最高/最低/末」（外加样本数）

        用途：价格监控、排名波动、任何有日内多次采样的指标——不是用相邻桶凑数。
        实现：单次按 ts 排序查询 + Python 归并（MySQL 5.7 无窗口函数，逐驱动写两套更脆）。
        """
        from datetime import timedelta
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        fmt = self._bucket_fmt(days) if bucket == "auto" else {
            "hour": "%Y-%m-%d %H:00", "day": "%Y-%m-%d", "week": "%Y-%W",
            "month": "%Y-%m"}.get(bucket, "%Y-%m-%d")
        bucket_expr = (self._backend.dialect.bucket("ts", fmt) if self._backend
                       else f"strftime('{fmt}', ts)")
        where = "workspace_id = ? AND metric = ? AND ts >= ?"
        params: list = [workspace_id, metric, since]
        if entity_id:
            where += " AND entity_id = ?"
            params.append(entity_id)
        dim_sql, dim_params = self._dim_where(dim_filters)
        where += dim_sql
        params.extend(dim_params)
        rows = await self._fetchall(
            f"""SELECT {bucket_expr} AS bucket, ts, value FROM metrics
                WHERE {where} ORDER BY ts ASC LIMIT ?""",
            tuple([*params, max(100, min(limit, 50000))]))
        out: dict[str, dict] = {}
        order: list[str] = []
        for r in rows:
            b = str(r["bucket"])
            v = float(r["value"] or 0)
            if b not in out:
                out[b] = {"bucket": b, "open": v, "high": v, "low": v, "close": v,
                          "n": 0, "first_ts": str(r["ts"]), "last_ts": str(r["ts"])}
                order.append(b)
            e = out[b]
            e["high"] = max(e["high"], v)
            e["low"] = min(e["low"], v)
            e["close"] = v                      # 已按 ts 升序 → 最后覆盖即为收盘
            e["last_ts"] = str(r["ts"])
            e["n"] += 1
        return [out[b] for b in order]

    async def metric_total(self, workspace_id: str, metric: str, days: float = 7,
                           agg: str = "sum", entity_id: str | None = None,
                           dim_filters: dict | None = None,
                           entity_allow: list[str] | None = None,
                           policy: tuple[str, list] | None = None) -> float:
        """单指标窗口内聚合值（KPI 卡）"""
        from datetime import timedelta
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        fn = {"sum": "SUM", "avg": "AVG", "max": "MAX"}.get(agg, "SUM")
        where = "workspace_id = ? AND metric = ? AND ts >= ?"
        params: list = [workspace_id, metric, since]
        if entity_id:
            where += " AND entity_id = ?"
            params.append(entity_id)
        if entity_allow:
            marks = ", ".join(["?"] * len(entity_allow[:50]))
            where += f" AND entity_id IN ({marks})"
            params.extend([str(v)[:80] for v in entity_allow[:50]])
        if policy and policy[0]:
            where += policy[0]
            params.extend(policy[1])
        dim_sql, dim_params = self._dim_where(dim_filters)
        where += dim_sql
        params.extend(dim_params)
        row = await self._fetchone(
            f"SELECT {fn}(value) AS v FROM metrics WHERE {where}", tuple(params))
        return float((row or {}).get("v") or 0)

    async def metric_totals(self, workspace_id: str, metrics: list[str],
                            days: float = 7, agg: str = "sum",
                            dim_filters: dict | None = None,
                            entity_allow: list[str] | None = None,
                            policy: tuple[str, list] | None = None) -> dict:
        """多指标聚合（一次查询，避免 N+1；同样受行级权限约束）"""
        if not metrics:
            return {}
        from datetime import timedelta
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        fn = {"sum": "SUM", "avg": "AVG", "max": "MAX"}.get(agg, "SUM")
        placeholders = ",".join("?" for _ in metrics)
        where = f"workspace_id = ? AND metric IN ({placeholders}) AND ts >= ?"
        params: list = [workspace_id, *metrics, since]
        if entity_allow:
            marks = ", ".join(["?"] * len(entity_allow[:50]))
            where += f" AND entity_id IN ({marks})"
            params.extend([str(v)[:80] for v in entity_allow[:50]])
        if policy and policy[0]:
            where += policy[0]
            params.extend(policy[1])
        dim_sql, dim_params = self._dim_where(dim_filters)
        where += dim_sql
        params.extend(dim_params)
        rows = await self._fetchall(
            f"""SELECT metric, {fn}(value) AS v, COUNT(*) AS n FROM metrics
                WHERE {where} GROUP BY metric""",
            tuple(params),
        )
        return {r["metric"]: {"value": float(r["v"] or 0), "n": r["n"]} for r in rows}

    async def metric_catalog(self, workspace_id: str, days: float = 90) -> list[dict]:
        """可用指标目录（即席探索用：指标名 + 数据量 + 主体 + 最近时间）"""
        from datetime import timedelta
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        rows = await self._fetchall(
            """SELECT metric, COUNT(*) AS n, COUNT(DISTINCT entity_id) AS entities,
                      MAX(ts) AS last_ts
               FROM metrics WHERE workspace_id = ? AND ts >= ?
               GROUP BY metric ORDER BY n DESC LIMIT 200""",
            (workspace_id, since),
        )
        out = []
        for r in rows:
            ents = await self._fetchall(
                """SELECT entity_id, COUNT(*) AS n FROM metrics
                   WHERE workspace_id = ? AND metric = ? AND ts >= ?
                   GROUP BY entity_id ORDER BY n DESC LIMIT 30""",
                (workspace_id, r["metric"], since))
            out.append({"metric": r["metric"], "count": r["n"],
                        "entity_count": r["entities"], "last_ts": r["last_ts"],
                        "entities": [e["entity_id"] for e in ents]})
        return out

    async def metric_breakdown(self, workspace_id: str, metric: str,
                               days: float = 7, limit: int = 20,
                               dim_filters: dict | None = None) -> list[dict]:
        """按主体（entity_id）聚合（横向条形图）"""
        from datetime import timedelta
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        dim_sql, dim_params = self._dim_where(dim_filters)
        rows = await self._fetchall(
            f"""SELECT entity_id, SUM(value) AS v, MAX(ts) AS last_ts, COUNT(*) AS n
               FROM metrics WHERE workspace_id = ? AND metric = ? AND ts >= ?{dim_sql}
               GROUP BY entity_id ORDER BY v DESC LIMIT ?""",
            tuple([workspace_id, metric, since, *dim_params, limit]),
        )
        return [{"entity_id": r["entity_id"], "value": float(r["v"] or 0),
                 "last_ts": r["last_ts"], "n": r["n"]} for r in rows]

    async def metric_dim_breakdown(self, workspace_id: str, metric: str, dim_key: str,
                                   days: float = 7, limit: int = 40,
                                   agg: str = "sum",
                                   dim_filters: dict | None = None) -> list[dict]:
        """按 dim_json 中的某个维度聚合（地域/渠道/设备等）

        用于网格地图、透视表与跨图联动。dim 缺失的行自动忽略。
        """
        from datetime import timedelta
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        expr = (self._backend.dialect.json_field("dim_json", dim_key)
                if self._backend else f"json_extract(dim_json, '$.{dim_key}')")
        fn = {"sum": "SUM", "avg": "AVG", "max": "MAX", "count": "COUNT"}.get(agg, "SUM")
        extra = ""
        params: list = [workspace_id, metric, since]
        for k2, v2 in (dim_filters or {}).items():
            if k2 == dim_key or v2 in (None, ""):
                continue
            expr2 = (self._backend.dialect.json_field("dim_json", k2)
                     if self._backend else f"json_extract(dim_json, '$.{k2}')")
            extra += f" AND {expr2} = ?"
            params.append(str(v2)[:80])
        params.append(limit)
        rows = await self._fetchall(
            f"""SELECT {expr} AS k, {fn}(value) AS v, COUNT(*) AS n
                FROM metrics
                WHERE workspace_id = ? AND metric = ? AND ts >= ?
                  AND {expr} IS NOT NULL{extra}
                GROUP BY k ORDER BY v DESC LIMIT ?""",
            tuple(params),
        )
        return [{"key": r["k"], "value": float(r["v"] or 0), "n": r["n"]} for r in rows]

    # ========== 透视 / 语义层 / 协作 / 告警 ==========

    async def pivot(self, workspace_id: str, metric: str, dim_key: str,
                    dim_key2: str = "", days: float = 30, limit: int = 12) -> dict:
        """二维透视（行列可切换）：dim_key × dim_key2 的聚合矩阵

        即席探索用：未指定第二维时退化为「维度 × 时间」。
        """
        store_rows = await self.metric_dim_breakdown(
            workspace_id, metric, dim_key, days=days, limit=limit)
        keys = [r["key"] for r in store_rows if r["key"]]
        if not dim_key2:
            series = await self.metric_series(workspace_id, metric, days=days)
            buckets = [s["bucket"] for s in series]
            matrix = []
            for k in keys:
                rows = await self.metric_series(workspace_id, metric, days=days,
                                                dim_filters={dim_key: k})
                vals = {r["bucket"]: r["value"] for r in rows}
                matrix.append([vals.get(b, 0.0) for b in buckets])
            return {"rows": keys, "cols": buckets, "matrix": matrix,
                    "metric": metric, "dim": dim_key, "dim2": "time"}
        from datetime import UTC, datetime, timedelta
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        e1 = (self._backend.dialect.json_field("dim_json", dim_key)
              if self._backend else f"json_extract(dim_json, '$.{dim_key}')")
        e2 = (self._backend.dialect.json_field("dim_json", dim_key2)
              if self._backend else f"json_extract(dim_json, '$.{dim_key2}')")
        sql_rows = await self._fetchall(
            f"""SELECT {e1} AS a, {e2} AS b, SUM(value) AS v
                FROM metrics WHERE workspace_id = ? AND metric = ? AND ts >= ?
                  AND {e1} IS NOT NULL AND {e2} IS NOT NULL
                GROUP BY a, b ORDER BY v DESC LIMIT 400""",
            (workspace_id, metric, since))
        cols: list[str] = []
        aset: list[str] = []
        cell: dict[tuple[str, str], float] = {}
        for r in sql_rows:
            a, b = str(r["a"])[:40], str(r["b"])[:40]
            if a not in aset:
                aset.append(a)
            if b not in cols:
                cols.append(b)
            cell[(a, b)] = float(r["v"] or 0)
        aset, cols = aset[:limit], cols[:limit]
        matrix = [[cell.get((a, b), 0.0) for b in cols] for a in aset]
        return {"rows": aset, "cols": cols, "matrix": matrix, "metric": metric,
                "dim": dim_key, "dim2": dim_key2}

    async def cube(self, workspace_id: str, metric: str, rows: list[str],
                   cols: str = "", *, days: float = 30, agg: str = "sum",
                   limit: int = 40) -> dict:
        """自由组合透视（拖拽式探索）：row_dims(1-2) × col_dim(0-1) → 矩阵

        rows = [] 时退化为「时间 × 指标」单列。维度名走白名单（json_field）。
        """
        from datetime import timedelta
        allowed = {"province", "country", "channel", "device", "step_name",
                   "keyword", "query", "source", "campaign", "competitor"}
        rows = [r for r in rows if r in allowed][:2]
        col_dim = cols if cols in allowed else ""
        fn = {"sum": "SUM", "avg": "AVG", "max": "MAX", "count": "COUNT"}.get(agg, "SUM")
        if not rows and not col_dim:
            series = await self.metric_series(workspace_id, metric, days=days,
                                             dim_filters=None, limit=limit)
            return {"rows": ["时间"], "cols": ["值"], "row_labels": [s["bucket"] for s in series],
                    "col_labels": [metric],
                    "matrix": [[s["value"]] for s in series],
                    "dim_rows": [], "dim_col": "", "metric": metric, "agg": agg}
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        exprs = [(self._backend.dialect.json_field("dim_json", k) if self._backend
                  else f"json_extract(dim_json, '$.{k}')") for k in rows]
        col_expr = ((self._backend.dialect.json_field("dim_json", col_dim)
                     if self._backend else f"json_extract(dim_json, '$.{col_dim}')")
                    if col_dim else ("DATE(ts)" if self.driver == "mysql"
                                     else "substr(ts, 1, 10)"))
        select_dims = ", ".join(f"{e} AS d{i}" for i, e in enumerate(exprs))
        prefix = f"{select_dims}, " if select_dims else ""
        not_null = " AND ".join(f"{e} IS NOT NULL" for e in exprs)
        sql_rows = await self._fetchall(
            f"""SELECT {prefix}{col_expr} AS c, {fn}(value) AS v
                FROM metrics WHERE workspace_id = ? AND metric = ? AND ts >= ?
                {('AND ' + not_null) if not_null else ''}
                GROUP BY {', '.join([f'd{i}' for i in range(len(exprs))] + ['c'])}
                ORDER BY v DESC LIMIT 800""",
            (workspace_id, metric, since))
        row_labels: list = []
        col_labels: list[str] = []
        cell: dict = {}
        for r in sql_rows:
            key = tuple(str(r.get(f"d{i}"))[:40] for i in range(len(exprs)))
            label = " / ".join(key) if key else "全部"
            c = str(r["c"])[:40]
            if label not in row_labels:
                row_labels.append(label)
            if c not in col_labels:
                col_labels.append(c)
            cell[(label, c)] = float(r["v"] or 0)
        row_labels, col_labels = row_labels[:limit], col_labels[:limit]
        matrix = [[cell.get((rl, c), 0.0) for c in col_labels] for rl in row_labels]
        return {"rows": rows, "cols": [col_dim or "时间"],
                "row_labels": row_labels, "col_labels": col_labels, "matrix": matrix,
                "dim_rows": rows, "dim_col": col_dim, "metric": metric, "agg": agg}

    # ---- 管理动作审计（合规：谁在何时改了什么）----

    async def record_admin(self, workspace_id: str, action: str, *,
                           actor: str = "", role: str = "",
                           target_type: str = "", target_id: str = "",
                           detail: dict | None = None, ip: str = "") -> None:
        """记录管理动作（失败不得影响主流程——调用方通常包 try/except）"""
        await self._execute(
            """INSERT INTO admin_audit (id, workspace_id, actor, role, action,
               target_type, target_id, detail_json, ip, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (generate_id(), workspace_id, actor[:191], role[:32], action[:64],
             target_type[:48], target_id[:191],
             json.dumps(detail or {}, ensure_ascii=False), ip[:64],
             datetime.now(UTC).isoformat()))
        await self._db.commit()

    async def list_admin_audit(self, workspace_id: str, *, action: str = "",
                               actor: str = "", limit: int = 200) -> list[dict]:
        where = "workspace_id = ?"
        params: list = [workspace_id]
        if action:
            where += " AND action = ?"
            params.append(action)
        if actor:
            where += " AND actor = ?"
            params.append(actor)
        rows = await self._fetchall(
            f"SELECT * FROM admin_audit WHERE {where} ORDER BY created_at DESC LIMIT ?",
            tuple([*params, min(limit, 1000)]))
        for r in rows:
            try:
                r["detail"] = json.loads(r.get("detail_json") or "{}")
            except Exception:
                r["detail"] = {}
        return rows

    async def dim_keys(self, workspace_id: str, metric: str = "",
                       candidates: tuple[str, ...] = (
                           "province", "country", "channel", "device", "campaign",
                           "keyword", "query", "step_name", "source")) -> list[str]:
        """实际有数据的维度键（一次查询/候选，供拖拽面板列出可用维度）"""
        out: list[str] = []
        for key in candidates[:12]:
            expr = (self._backend.dialect.json_field("dim_json", key) if self._backend
                    else f"json_extract(dim_json, '$.{key}')")
            where = "workspace_id = ? AND metric = ?" if metric else "workspace_id = ?"
            params = [workspace_id, metric] if metric else [workspace_id]
            row = await self._fetchone(
                f"""SELECT COUNT(*) AS n FROM metrics
                    WHERE {where} AND {expr} IS NOT NULL""", tuple(params))
            if row and int(row.get("n") or 0) > 0:
                out.append(key)
        return out

    # ---- 语义层：指标口径（定义/版本/责任人）----

    async def upsert_metric_def(self, workspace_id: str, name: str, **fields) -> dict:
        now = datetime.now(UTC).isoformat()
        cur = await self._fetchone(
            "SELECT * FROM metric_defs WHERE workspace_id = ? AND name = ?",
            (workspace_id, name))
        if cur:
            sets, params = [], []
            for col in ("label", "expr", "unit", "owner", "notes"):
                if col in fields and fields[col] is not None:
                    sets.append(f"{col} = ?")
                    params.append(str(fields[col]))
            # 口径变更 → 版本自增（可追溯历史）
            if fields.get("expr") and fields["expr"] != (cur["expr"] or ""):
                sets.append("version = version + 1")
            sets.append("status = ?")
            params.append(str(fields.get("status") or cur["status"] or "active"))
            sets.append("updated_at = ?")
            params.append(now)
            params.extend([workspace_id, name])
            await self._execute(
                f"UPDATE metric_defs SET {', '.join(sets)} "
                f"WHERE workspace_id = ? AND name = ?", tuple(params))
        else:
            await self._execute(
                """INSERT INTO metric_defs (id, workspace_id, name, label, expr, unit,
                   owner, version, status, notes, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'active', ?, ?, ?)""",
                (new_id(), workspace_id, name, str(fields.get("label") or name),
                 str(fields.get("expr") or ""), str(fields.get("unit") or ""),
                 str(fields.get("owner") or ""), str(fields.get("notes") or ""),
                 now, now))
        await self._db.commit()
        return await self._fetchone(
            "SELECT * FROM metric_defs WHERE workspace_id = ? AND name = ?",
            (workspace_id, name)) or {}

    async def list_metric_defs(self, workspace_id: str) -> list[dict]:
        return await self._fetchall(
            "SELECT * FROM metric_defs WHERE workspace_id = ? ORDER BY name",
            (workspace_id,))

    # ---- 协作：评论 ----

    async def add_comment(self, workspace_id: str, target_type: str, target_id: str,
                          body: str, author: str = "") -> dict:
        cid = new_id()
        await self._execute(
            """INSERT INTO comments (id, workspace_id, target_type, target_id,
               author, body, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (cid, workspace_id, target_type[:32], target_id[:96], author[:96],
             body[:4000], datetime.now(UTC).isoformat()))
        await self._db.commit()
        return {"id": cid, "target_type": target_type, "target_id": target_id,
                "author": author, "body": body}

    async def list_comments(self, workspace_id: str, target_type: str = "",
                            target_id: str = "", limit: int = 200) -> list[dict]:
        where = "workspace_id = ?"
        params: list = [workspace_id]
        if target_type:
            where += " AND target_type = ?"
            params.append(target_type)
        if target_id:
            where += " AND target_id = ?"
            params.append(target_id)
        return await self._fetchall(
            f"SELECT * FROM comments WHERE {where} ORDER BY created_at DESC LIMIT ?",
            tuple([*params, limit]))

    # ---- 告警规则 ----

    async def upsert_alert_rule(self, workspace_id: str, rule: dict) -> dict:
        now = datetime.now(UTC).isoformat()
        rid = rule.get("id") or new_id()
        existing = await self._fetchone(
            "SELECT id FROM alert_rules WHERE workspace_id = ? AND id = ?",
            (workspace_id, rid))
        cols = ("name", "metric", "op", "threshold", "window_days", "dims_json",
                "routes_json", "escalation_json", "enabled")
        vals = [str(rule.get("name") or "未命名规则")[:191],
                str(rule.get("metric") or "")[:96], str(rule.get("op") or "gt")[:8],
                float(rule.get("threshold") or 0), float(rule.get("window_days") or 7),
                json.dumps(rule.get("dims") or {}, ensure_ascii=False),
                json.dumps(rule.get("routes") or [], ensure_ascii=False),
                json.dumps(rule.get("escalation") or {}, ensure_ascii=False),
                1 if rule.get("enabled", True) else 0]
        if existing:
            sets = ", ".join(f"{c} = ?" for c in cols)
            await self._execute(
                f"UPDATE alert_rules SET {sets}, updated_at = ? "
                f"WHERE workspace_id = ? AND id = ?",
                tuple([*vals, now, workspace_id, rid]))
        else:
            await self._execute(
                f"""INSERT INTO alert_rules (id, workspace_id, {', '.join(cols)},
                    last_fired_at, created_at, updated_at)
                    VALUES (?, ?, {', '.join(['?'] * len(cols))}, '', ?, ?)""",
                tuple([rid, workspace_id, *vals, now, now]))
        await self._db.commit()
        return await self._fetchone(
            "SELECT * FROM alert_rules WHERE workspace_id = ? AND id = ?",
            (workspace_id, rid)) or {}

    async def list_alert_rules(self, workspace_id: str,
                               enabled_only: bool = False) -> list[dict]:
        where = "workspace_id = ?"
        if enabled_only:
            where += " AND enabled = 1"
        return await self._fetchall(
            f"SELECT * FROM alert_rules WHERE {where} ORDER BY created_at DESC",
            (workspace_id,))

    async def delete_alert_rule(self, workspace_id: str, rule_id: str) -> bool:
        cur = await self._execute(
            "DELETE FROM alert_rules WHERE workspace_id = ? AND id = ?",
            (workspace_id, rule_id))
        await self._db.commit()
        return cur.rowcount > 0

    async def mark_rule_fired(self, workspace_id: str, rule_id: str) -> None:
        await self._execute(
            "UPDATE alert_rules SET last_fired_at = ? WHERE workspace_id = ? AND id = ?",
            (datetime.now(UTC).isoformat(), workspace_id, rule_id))
        await self._db.commit()

    # ========== Competitors（M3: 竞品档案）==========

    def _parse_competitor_row(self, row: dict) -> dict:
        for field in ["pricing_json", "product_lines_json", "social_json",
                      "monitors_json", "seo_json"]:
            row[field] = json.loads(row[field]) if row[field] else ([] if field == "pricing_json" else {} if field != "monitors_json" else [])
        return row

    async def upsert_competitor(self, workspace_id: str, domain: str, profile: dict) -> dict:
        """创建或更新竞品档案"""
        existing = await self._fetchone(
            "SELECT id FROM competitors WHERE workspace_id = ? AND domain = ?",
            (workspace_id, domain),
        )
        now = datetime.now(UTC).isoformat()
        if existing:
            fields = []
            params: list = []
            mapping = {
                "name": "name", "positioning": "positioning", "status": "status",
            }
            for key, col in mapping.items():
                if key in profile:
                    fields.append(f"{col} = ?")
                    params.append(profile[key])
            for key, col in [("pricing", "pricing_json"), ("product_lines", "product_lines_json"),
                             ("social", "social_json"), ("monitors", "monitors_json"),
                             ("seo", "seo_json")]:
                if key in profile:
                    fields.append(f"{col} = ?")
                    params.append(to_json(profile[key]))
            params.extend([existing["id"]])
            await self._execute(
                f"UPDATE competitors SET {', '.join(fields) if fields else 'name = name'} WHERE id = ?",
                tuple(params),
            )
            await self._db.commit()
            row = await self._fetchone("SELECT * FROM competitors WHERE id = ?", (existing["id"],))
            return self._parse_competitor_row(dict(row))
        else:
            cid = generate_id()
            await self._execute(
                """INSERT INTO competitors (id, workspace_id, domain, name, positioning,
                   pricing_json, product_lines_json, social_json, monitors_json, seo_json, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (cid, workspace_id, domain,
                 profile.get("name", domain), profile.get("positioning", ""),
                 to_json(profile.get("pricing", [])),
                 to_json(profile.get("product_lines", [])),
                 to_json(profile.get("social", {})),
                 to_json(profile.get("monitors", [])),
                 to_json(profile.get("seo", {})),
                 profile.get("status", "active"), now),
            )
            await self._db.commit()
            row = await self._fetchone("SELECT * FROM competitors WHERE id = ?", (cid,))
            return self._parse_competitor_row(dict(row))

    async def get_competitor(self, workspace_id: str, domain: str) -> dict | None:
        row = await self._fetchone(
            "SELECT * FROM competitors WHERE workspace_id = ? AND domain = ?",
            (workspace_id, domain),
        )
        return self._parse_competitor_row(dict(row)) if row else None

    async def list_competitors(self, workspace_id: str) -> list[dict]:
        rows = await self._fetchall(
            "SELECT * FROM competitors WHERE workspace_id = ? ORDER BY created_at",
            (workspace_id,),
        )
        return [self._parse_competitor_row(row) for row in rows]

    # ========== Keyword Ranks（M3: 排名追踪）==========

    async def save_keyword_ranks(self, workspace_id: str, competitor_domain: str,
                                 ranks: list[dict], checked_at: datetime | None = None) -> int:
        """批量保存关键词排名快照"""
        checked_at = (checked_at or datetime.now(UTC)).isoformat()
        for r in ranks:
            await self._execute(
                """INSERT INTO keyword_ranks (id, workspace_id, competitor_domain, keyword,
                   position, volume, url, is_mine, checked_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (generate_id(), workspace_id, competitor_domain,
                 r.get("keyword", ""), r.get("position"), r.get("volume", 0),
                 r.get("url", ""), 1 if r.get("is_mine") else 0, checked_at),
            )
        await self._db.commit()
        return len(ranks)

    async def latest_keyword_ranks(self, workspace_id: str, competitor_domain: str | None = None,
                                   mine_only: bool = False) -> list[dict]:
        """最近一次检查的关键词排名"""
        where = "workspace_id = ?"
        params: list = [workspace_id]
        if competitor_domain:
            where += " AND competitor_domain = ?"
            params.append(competitor_domain)
        if mine_only:
            where += " AND is_mine = 1"
        rows = await self._fetchall(
            f"""SELECT * FROM keyword_ranks
                WHERE {where}
                ORDER BY checked_at DESC, position ASC""",
            tuple(params),
        )
        # 只取最近一次 checked_at 的数据
        if not rows:
            return []
        latest_ts = rows[0]["checked_at"]
        return [r for r in rows if r["checked_at"] == latest_ts]

    # ========== Journey Events（M3: 旅程重建数据底座）==========

    async def save_journey_event(self, workspace_id: str, identity: str, stage: str,
                                 event: str, props: dict | None = None,
                                 source: str = "openflow", ts: str | None = None) -> str:
        eid = generate_id()
        await self._execute(
            """INSERT INTO journey_events (id, workspace_id, identity, stage, event, props_json, source, ts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (eid, workspace_id, identity, stage, event,
             to_json(props or {}), source, ts or datetime.now(UTC).isoformat()),
        )
        await self._db.commit()
        return eid

    async def list_journey_events(self, workspace_id: str, identity: str | None = None,
                                  stage: str | None = None, limit: int = 500) -> list[dict]:
        query = "SELECT * FROM journey_events WHERE workspace_id = ?"
        params: list = [workspace_id]
        if identity:
            query += " AND identity = ?"
            params.append(identity)
        if stage:
            query += " AND stage = ?"
            params.append(stage)
        query += " ORDER BY ts ASC LIMIT ?"
        params.append(limit)
        rows = await self._fetchall(query, tuple(params))
        for row in rows:
            row["props_json"] = json.loads(row["props_json"]) if row["props_json"] else {}
        return rows

    # ========== Metrics 查询（舆情看板等）==========

    async def latest_metrics(self, workspace_id: str, metrics: list[str]) -> list[dict]:
        """每个 (entity_id, metric) 取最近一条（看板用，避免全量时序）"""
        if not metrics:
            return []
        placeholders = ",".join("?" for _ in metrics)
        rows = await self._fetchall(
            f"""SELECT m.* FROM metrics m
                JOIN (SELECT entity_id, metric, MAX(ts) AS mts FROM metrics
                      WHERE workspace_id = ? AND metric IN ({placeholders})
                      GROUP BY entity_id, metric) latest
                ON m.entity_id = latest.entity_id AND m.metric = latest.metric AND m.ts = latest.mts
                WHERE m.workspace_id = ?
                ORDER BY m.ts DESC""",
            tuple([workspace_id, *metrics, workspace_id]),
        )
        for r in rows:
            r["dim_json"] = json.loads(r["dim_json"]) if r["dim_json"] else {}
        return rows

    # ========== Monitors（M6: 监控任务 CRUD）==========

    def _parse_monitor_row(self, row: dict) -> dict:
        row["target_json"] = json.loads(row["target_json"]) if row["target_json"] else {}
        return row

    async def create_monitor(self, workspace_id: str, kind: str,
                             target: dict, schedule_cron: str = "0 */6 * * *") -> dict:
        """创建监控任务（kind: site_change | keyword | brand_mention | journey）"""
        mid = generate_id()
        await self._execute(
            """INSERT INTO monitors (id, workspace_id, kind, target_json, schedule_cron, state, created_at)
               VALUES (?, ?, ?, ?, ?, 'idle', ?)""",
            (mid, workspace_id, kind, to_json(target), schedule_cron,
             datetime.now(UTC).isoformat()),
        )
        await self._db.commit()
        row = await self._fetchone("SELECT * FROM monitors WHERE id = ?", (mid,))
        return self._parse_monitor_row(dict(row))

    async def get_monitor(self, workspace_id: str, monitor_id: str) -> dict | None:
        row = await self._fetchone(
            "SELECT * FROM monitors WHERE workspace_id = ? AND id = ?",
            (workspace_id, monitor_id),
        )
        return self._parse_monitor_row(dict(row)) if row else None

    async def update_monitor_state(self, monitor_id: str, state: str,
                                   last_run_at: datetime | None = None) -> bool:
        fields = ["state = ?"]
        params: list = [state]
        if last_run_at is not None:
            fields.append("last_run_at = ?")
            params.append(last_run_at.isoformat())
        params.append(monitor_id)
        await self._execute(
            f"UPDATE monitors SET {', '.join(fields)} WHERE id = ?", tuple(params)
        )
        await self._db.commit()
        return True

    async def delete_monitor(self, workspace_id: str, monitor_id: str) -> bool:
        cur = await self._execute(
            "DELETE FROM monitors WHERE workspace_id = ? AND id = ?",
            (workspace_id, monitor_id),
        )
        await self._db.commit()
        return cur.rowcount > 0

    async def list_monitors_full(self, workspace_id: str) -> list[dict]:
        rows = await self._fetchall(
            "SELECT * FROM monitors WHERE workspace_id = ? ORDER BY created_at DESC",
            (workspace_id,),
        )
        return [self._parse_monitor_row(row) for row in rows]


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
