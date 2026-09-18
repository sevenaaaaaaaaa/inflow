"""隐私合规：数据主体请求（导出/删除）+ 留存策略

对标企业采购常问的三件事：
1. **数据主体访问/导出**（DSAR）：按邮箱导出该主体的全部数据（JSON）
2. **数据主体删除**（RTBF）：删除/匿名化该主体的数据，并保留**删除动作本身的审计**
3. **留存策略**：按表设置保留天数（metrics/events/journey_events/comments/audit），
   定期清理并留审计痕迹

诚实边界：
- 「删除」优先**匿名化 + 解除关联**（保统计口径可用），可用 `purge=True` 走物理删除；
- 只覆盖本系统自有表；采集自第三方的原始数据（raw_records）按同样策略清理；
- 已被其它主体共享的聚合指标不含个人标识，不做删除（否则破坏统计事实）。
"""

import contextlib
from datetime import UTC, datetime, timedelta

DEFAULT_RETENTION = {
    "metrics_days": 730,        # 指标明细保留 2 年
    "raw_records_days": 180,
    "events_days": 180,
    "journey_events_days": 365,
    "comments_days": 730,
    "insights_days": 0,         # 0 = 不自动清理（业务资产）
    "admin_audit_days": 1095,   # 审计 3 年（合规要求通常 ≥1 年）
}
ANON_AUTHOR = "已删除用户"


class PrivacyError(Exception):
    pass


async def export_subject(workspace_id: str, email: str, *, limit: int = 5000) -> dict:
    """DSAR：导出某主体的全部数据"""
    email = (email or "").strip().lower()
    if not email:
        raise PrivacyError("需要 email")
    from ..core.store import get_store
    store = await get_store()
    user = await store._fetchone(
        "SELECT id, email, name, role, workspace_id, created_at FROM users "
        "WHERE email = ?", (email,))
    journey = await store._fetchall(
        """SELECT stage, event, props_json, source, ts FROM journey_events
           WHERE workspace_id = ? AND identity = ? ORDER BY ts DESC LIMIT ?""",
        (workspace_id, email, limit))
    comments = await store._fetchall(
        """SELECT target_type, target_id, body, created_at FROM comments
           WHERE workspace_id = ? AND author = ? ORDER BY created_at DESC LIMIT ?""",
        (workspace_id, email, limit))
    # 洞察/动作不含主体字段，但 evidence 可能引用邮箱 → 显式扫描并报告
    insights = await store._fetchall(
        """SELECT id, type, title, summary, created_at FROM insights
           WHERE workspace_id = ? AND (evidence_json LIKE ? OR summary LIKE ?)
           ORDER BY created_at DESC LIMIT 200""",
        (workspace_id, f"%{email}%", f"%{email}%"))
    return {
        "subject": {"email": email, "user": dict(user) if user else None},
        "journey_events": [dict(r) for r in journey],
        "comments": [dict(r) for r in comments],
        "referenced_in_insights": [dict(r) for r in insights],
        "exported_at": datetime.now(UTC).isoformat(),
        "note": "本文件含个人数据，请通过安全渠道交付并设置有效期",
    }


async def _scan_references(store, workspace_id: str, email: str) -> dict:
    counts = {}
    for table, column in (("insights", "evidence_json"), ("insights", "summary"),
                          ("actions", "result_json"), ("raw_records", "payload_json"),
                          ("comments", "body")):
        row = await store._fetchone(
            f"SELECT COUNT(*) AS n FROM {table} WHERE workspace_id = ? AND {column} LIKE ?",
            (workspace_id, f"%{email}%"))
        key = f"{table}.{column}"
        counts[key] = int((row or {}).get("n") or 0)
    return counts


async def erase_subject(workspace_id: str, email: str, *, purge: bool = False,
                        dry_run: bool = True, actor: str = "") -> dict:
    """RTBF：删除/匿名化主体数据（默认 dry-run，先看清影响面）"""
    email = (email or "").strip().lower()
    if not email:
        raise PrivacyError("需要 email")
    from ..core.store import get_store
    store = await get_store()
    refs = await _scan_references(store, workspace_id, email)
    journey = await store._fetchone(
        "SELECT COUNT(*) AS n FROM journey_events WHERE workspace_id = ? AND identity = ?",
        (workspace_id, email))
    comments = await store._fetchone(
        "SELECT COUNT(*) AS n FROM comments WHERE workspace_id = ? AND author = ?",
        (workspace_id, email))
    user = await store._fetchone("SELECT id FROM users WHERE email = ?", (email,))
    plan = {
        "journey_events": int((journey or {}).get("n") or 0),
        "comments": int((comments or {}).get("n") or 0),
        "user_row": bool(user),
        "mode": "purge" if purge else "anonymize",
        "references_found": refs,
        "notes": [
            "聚合指标/洞察本身不含个人标识，不做删除（否则破坏统计事实）",
            "如有 references_found，需人工确认是否需进一步脱敏（本工具不猜测语义）",
        ],
    }
    if dry_run:
        return {"dry_run": True, "plan": plan}
    if purge:
        await store._execute(
            "DELETE FROM journey_events WHERE workspace_id = ? AND identity = ?",
            (workspace_id, email))
        await store._execute(
            "DELETE FROM comments WHERE workspace_id = ? AND author = ?",
            (workspace_id, email))
    else:
        # 匿名化：解除身份关联，保留统计形态
        await store._execute(
            "UPDATE journey_events SET identity = ? WHERE workspace_id = ? AND identity = ?",
            (f"anon-{abs(hash(email)) % 10**8}", workspace_id, email))
        await store._execute(
            "UPDATE comments SET author = ? WHERE workspace_id = ? AND author = ?",
            (ANON_AUTHOR, workspace_id, email))
    if user:
        await store._execute(
            """UPDATE users SET email = ?, name = ?, active = 0 WHERE id = ?""",
            (f"deleted+{abs(hash(email)) % 10**8}@invalid", ANON_AUTHOR, user["id"]))
    await store._db.commit()
    # 审计：删除动作本身必须留痕（合规）；审计失败不阻断删除
    with contextlib.suppress(Exception):
        await store.record_admin(workspace_id, "privacy.subject_erase",
                                 actor=actor or "本地用户", target_type="subject",
                                 target_id=email.split("@")[0][:3] + "***",
                                 detail={"mode": plan["mode"], "plan": plan})
    return {"dry_run": False, "done": plan}


async def retention_sweep(workspace_id: str, *, dry_run: bool = True,
                          override: dict | None = None) -> dict:
    """留存策略清理：按表保留天数删除过期数据（默认 dry-run）"""
    from ..core.store import get_store
    store = await get_store()
    ws = await store.get_workspace(workspace_id)
    policy = {**DEFAULT_RETENTION,
              **((ws.settings_json or {}).get("retention") or {}),
              **(override or {})}
    tables = {
        "metrics": ("ts", "metrics_days"),
        "raw_records": ("captured_at", "raw_records_days"),
        "journey_events": ("ts", "journey_events_days"),
        "comments": ("created_at", "comments_days"),
        "insights": ("created_at", "insights_days"),
        "admin_audit": ("created_at", "admin_audit_days"),
    }
    result = {}
    for table, (column, key) in tables.items():
        days = int(policy.get(key) or 0)
        if days <= 0:
            result[table] = {"days": 0, "skipped": "保留策略为 0（不自动清理）"}
            continue
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        row = await store._fetchone(
            f"SELECT COUNT(*) AS n FROM {table} WHERE workspace_id = ? AND {column} < ?",
            (workspace_id, cutoff))
        n = int((row or {}).get("n") or 0)
        if dry_run or n == 0:
            result[table] = {"days": days, "would_delete": n,
                             "dry_run": dry_run}
            continue
        await store._execute(
            f"DELETE FROM {table} WHERE workspace_id = ? AND {column} < ?",
            (workspace_id, cutoff))
        result[table] = {"days": days, "deleted": n}
    if not dry_run:
        await store._db.commit()
        with contextlib.suppress(Exception):
            await store.record_admin(workspace_id, "privacy.retention_sweep",
                                     actor="scheduler", target_type="retention",
                                     detail={"policy": policy, "result": result})
    return {"dry_run": dry_run, "policy": policy, "result": result}
