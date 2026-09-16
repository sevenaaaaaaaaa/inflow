"""Insight Flow SQLite 存储层"""

import json
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
            """INSERT INTO feedback (id, workspace_id, action_id, metric, before, after, delta, verdict, evaluated_at)
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
