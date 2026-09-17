"""Insight Flow MySQL 迁移脚本

与 SQLite 版等价，但按 MySQL 5.7 约束改写（对齐 OpenFlow 迁移教训）：
1. **索引列必须是 VARCHAR**（MySQL 不能对裸 TEXT 建索引/唯一键）
2. **无部分索引**（5.7）→ metrics 去重改用完整 UNIQUE KEY；
   仅对"全新库"成立（与 OpenFlow 一样「MySQL 从零开始」，无历史空值行）
3. **`CREATE INDEX IF NOT EXISTS` 不支持** → 索引内联写在 CREATE TABLE 里
4. 一律 InnoDB + utf8mb4
"""

MYSQL_MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS workspaces (
        id VARCHAR(64) PRIMARY KEY,
        name VARCHAR(255) NOT NULL,
        stage VARCHAR(8) NOT NULL DEFAULT 'S0',
        maturity_level VARCHAR(8) NOT NULL DEFAULT 'L0',
        settings_json TEXT,
        created_at VARCHAR(40) NOT NULL,
        updated_at VARCHAR(40) NOT NULL,
        INDEX idx_workspaces_name (name)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,
    """
    CREATE TABLE IF NOT EXISTS sources (
        id VARCHAR(64) PRIMARY KEY,
        workspace_id VARCHAR(64) NOT NULL,
        plugin_id VARCHAR(128) NOT NULL,
        config_enc TEXT,
        quota_ledger_json TEXT,
        status VARCHAR(32) NOT NULL DEFAULT 'active',
        created_at VARCHAR(40) NOT NULL,
        INDEX idx_sources_workspace (workspace_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,
    """
    CREATE TABLE IF NOT EXISTS monitors (
        id VARCHAR(64) PRIMARY KEY,
        workspace_id VARCHAR(64) NOT NULL,
        kind VARCHAR(48) NOT NULL,
        target_json TEXT,
        schedule_cron VARCHAR(64) NOT NULL DEFAULT '0 */6 * * *',
        last_run_at VARCHAR(40) NULL,
        state VARCHAR(24) NOT NULL DEFAULT 'idle',
        created_at VARCHAR(40) NOT NULL,
        INDEX idx_monitors_workspace (workspace_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,
    """
    CREATE TABLE IF NOT EXISTS raw_records (
        id VARCHAR(64) PRIMARY KEY,
        workspace_id VARCHAR(64) NOT NULL,
        source_id VARCHAR(64) NOT NULL,
        monitor_id VARCHAR(64) NOT NULL,
        kind VARCHAR(64) NOT NULL,
        payload_json LONGTEXT,
        captured_at VARCHAR(40) NOT NULL,
        INDEX idx_raw_records_workspace (workspace_id),
        INDEX idx_raw_records_source (source_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,
    """
    CREATE TABLE IF NOT EXISTS metrics (
        id VARCHAR(64) PRIMARY KEY,
        workspace_id VARCHAR(64) NOT NULL,
        entity_type VARCHAR(32) NOT NULL,
        entity_id VARCHAR(255) NOT NULL,
        metric VARCHAR(128) NOT NULL,
        value DOUBLE NOT NULL,
        dim_json TEXT,
        ts VARCHAR(40) NOT NULL,
        monitor_id VARCHAR(64) NOT NULL DEFAULT '',
        window_key VARCHAR(32) NOT NULL DEFAULT '',
        INDEX idx_metrics_entity (entity_type, entity_id),
        INDEX idx_metrics_ws_metric_ts (workspace_id, metric, ts),
        INDEX idx_metrics_ws_entity_metric_ts (workspace_id, entity_id, metric, ts),
        INDEX idx_metrics_window (workspace_id, metric, window_key)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,
    """
    CREATE TABLE IF NOT EXISTS insights (
        id VARCHAR(64) PRIMARY KEY,
        workspace_id VARCHAR(64) NOT NULL,
        type VARCHAR(64) NOT NULL,
        title VARCHAR(255) NOT NULL,
        summary TEXT,
        severity VARCHAR(16) NOT NULL DEFAULT 'medium',
        confidence DOUBLE NOT NULL DEFAULT 0.5,
        evidence_json LONGTEXT,
        models_json TEXT,
        actions_json TEXT,
        stage_tags_json TEXT,
        status VARCHAR(24) NOT NULL DEFAULT 'new',
        created_at VARCHAR(40) NOT NULL,
        verified_at VARCHAR(40) NULL,
        INDEX idx_insights_workspace (workspace_id),
        INDEX idx_insights_status (status),
        INDEX idx_insights_severity (severity),
        INDEX idx_insights_type (type)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,
    """
    CREATE TABLE IF NOT EXISTS actions (
        id VARCHAR(64) PRIMARY KEY,
        workspace_id VARCHAR(64) NOT NULL,
        insight_id VARCHAR(64) NOT NULL,
        action_type VARCHAR(64) NOT NULL,
        target_ref VARCHAR(512) NULL,
        params_json TEXT,
        state VARCHAR(24) NOT NULL DEFAULT 'pending',
        dispatched_at VARCHAR(40) NULL,
        result_json TEXT,
        verify_window_until VARCHAR(40) NULL,
        baseline_json TEXT,
        created_at VARCHAR(40) NOT NULL,
        INDEX idx_actions_workspace (workspace_id),
        INDEX idx_actions_insight (insight_id),
        INDEX idx_actions_state (state)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,
    """
    CREATE TABLE IF NOT EXISTS feedback (
        id VARCHAR(64) PRIMARY KEY,
        workspace_id VARCHAR(64) NOT NULL,
        action_id VARCHAR(64) NOT NULL,
        metric VARCHAR(128) NOT NULL,
        `before` DOUBLE NOT NULL,
        `after` DOUBLE NOT NULL,
        delta DOUBLE NOT NULL,
        verdict VARCHAR(24) NOT NULL,
        evaluated_at VARCHAR(40) NOT NULL,
        INDEX idx_feedback_workspace (workspace_id),
        INDEX idx_feedback_action (action_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,
    """
    CREATE TABLE IF NOT EXISTS competitors (
        id VARCHAR(64) PRIMARY KEY,
        workspace_id VARCHAR(64) NOT NULL,
        domain VARCHAR(255) NOT NULL,
        name VARCHAR(255) NOT NULL DEFAULT '',
        positioning TEXT,
        pricing_json TEXT,
        product_lines_json TEXT,
        social_json TEXT,
        monitors_json TEXT,
        seo_json TEXT,
        status VARCHAR(24) NOT NULL DEFAULT 'active',
        created_at VARCHAR(40) NOT NULL,
        UNIQUE KEY uk_competitors_ws_domain (workspace_id, domain),
        INDEX idx_competitors_workspace (workspace_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,
    """
    CREATE TABLE IF NOT EXISTS keyword_ranks (
        id VARCHAR(64) PRIMARY KEY,
        workspace_id VARCHAR(64) NOT NULL,
        competitor_domain VARCHAR(255) NOT NULL,
        keyword VARCHAR(255) NOT NULL,
        position INT NULL,
        volume INT NULL,
        url VARCHAR(512) NOT NULL DEFAULT '',
        is_mine TINYINT NOT NULL DEFAULT 0,
        checked_at VARCHAR(40) NOT NULL,
        INDEX idx_keyword_ranks_ws (workspace_id, competitor_domain)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,
    """
    CREATE TABLE IF NOT EXISTS journey_events (
        id VARCHAR(64) PRIMARY KEY,
        workspace_id VARCHAR(64) NOT NULL,
        identity VARCHAR(255) NOT NULL,
        stage VARCHAR(48) NOT NULL,
        event VARCHAR(64) NOT NULL,
        props_json TEXT,
        source VARCHAR(48) NOT NULL DEFAULT 'openflow',
        ts VARCHAR(40) NOT NULL,
        INDEX idx_journey_identity (workspace_id, identity),
        INDEX idx_journey_stage (workspace_id, stage)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,
    """
    CREATE TABLE IF NOT EXISTS users (
        id VARCHAR(64) PRIMARY KEY,
        email VARCHAR(255) NOT NULL,
        password_hash VARCHAR(255) NOT NULL,
        name VARCHAR(128) NOT NULL DEFAULT '',
        role VARCHAR(24) NOT NULL DEFAULT 'owner',
        workspace_id VARCHAR(64) NULL,
        created_at VARCHAR(40) NOT NULL,
        UNIQUE KEY uk_users_email (email)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        token VARCHAR(128) PRIMARY KEY,
        user_id VARCHAR(64) NOT NULL,
        created_at VARCHAR(40) NOT NULL,
        expires_at VARCHAR(40) NOT NULL,
        INDEX idx_sessions_user (user_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,
    """
    CREATE TABLE IF NOT EXISTS subscriptions (
        id VARCHAR(64) PRIMARY KEY,
        workspace_id VARCHAR(64) NOT NULL,
        name VARCHAR(255) NOT NULL,
        channels_json TEXT,
        target_json TEXT,
        filters_json TEXT,
        mode VARCHAR(24) NOT NULL DEFAULT 'immediate',
        enabled TINYINT NOT NULL DEFAULT 1,
        created_at VARCHAR(40) NOT NULL,
        INDEX idx_subscriptions_ws (workspace_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,
    # V4: 预聚合 + 语义层 + 协作 + 告警（MySQL 版）
    """
    CREATE TABLE IF NOT EXISTS metric_daily (
        id VARCHAR(64) PRIMARY KEY,
        workspace_id VARCHAR(64) NOT NULL,
        metric VARCHAR(96) NOT NULL,
        day VARCHAR(16) NOT NULL,
        entity_type VARCHAR(32) NOT NULL DEFAULT 'site',
        entity_id VARCHAR(191) NOT NULL DEFAULT 'main',
        dim_key VARCHAR(191) NOT NULL DEFAULT '',
        agg_sum DOUBLE NOT NULL DEFAULT 0,
        agg_avg DOUBLE NOT NULL DEFAULT 0,
        agg_max DOUBLE NOT NULL DEFAULT 0,
        n INT NOT NULL DEFAULT 0,
        updated_at VARCHAR(40) NOT NULL,
        KEY idx_metric_daily_lookup (workspace_id, metric, day),
        UNIQUE KEY uq_metric_daily (workspace_id, metric, day, entity_type, entity_id, dim_key)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS metric_defs (
        id VARCHAR(64) PRIMARY KEY,
        workspace_id VARCHAR(64) NOT NULL,
        name VARCHAR(96) NOT NULL,
        label VARCHAR(191) NOT NULL DEFAULT '',
        expr TEXT,
        unit VARCHAR(32) NOT NULL DEFAULT '',
        owner VARCHAR(96) NOT NULL DEFAULT '',
        version INT NOT NULL DEFAULT 1,
        status VARCHAR(24) NOT NULL DEFAULT 'active',
        notes TEXT,
        created_at VARCHAR(40) NOT NULL,
        updated_at VARCHAR(40) NOT NULL,
        KEY idx_metric_defs_ws (workspace_id, name)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS comments (
        id VARCHAR(64) PRIMARY KEY,
        workspace_id VARCHAR(64) NOT NULL,
        target_type VARCHAR(32) NOT NULL,
        target_id VARCHAR(96) NOT NULL,
        author VARCHAR(96) NOT NULL DEFAULT '',
        body TEXT,
        created_at VARCHAR(40) NOT NULL,
        KEY idx_comments_target (workspace_id, target_type, target_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS alert_rules (
        id VARCHAR(64) PRIMARY KEY,
        workspace_id VARCHAR(64) NOT NULL,
        name VARCHAR(191) NOT NULL,
        metric VARCHAR(96) NOT NULL,
        op VARCHAR(8) NOT NULL DEFAULT 'gt',
        threshold DOUBLE NOT NULL DEFAULT 0,
        window_days DOUBLE NOT NULL DEFAULT 7,
        dims_json TEXT,
        routes_json TEXT,
        escalation_json TEXT,
        enabled INT NOT NULL DEFAULT 1,
        last_fired_at VARCHAR(40) NOT NULL DEFAULT '',
        created_at VARCHAR(40) NOT NULL,
        updated_at VARCHAR(40) NOT NULL,
        KEY idx_alert_rules_ws (workspace_id, enabled)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
]
