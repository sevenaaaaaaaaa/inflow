"""数据质量 SLA：新鲜度 / 完整性 / 断点续采 / 失败告警

为什么需要：采集链路静默失败是"最丢人的事故"——看板还在，数据停了。这里把
「数据是否可信」变成可查询、可告警、可补救的能力：

- **新鲜度**（freshness）：每个指标最近一次数据距今多久 vs SLA（默认 26h，可按指标配）
- **完整性**（completeness）：窗口内应有 vs 实有数据点、缺口日列表、连续零值（疑似断采）
- **断点续采**（backfill）：按缺口窗口重跑对应监控任务（复用调度器幂等窗口键）
- **告警**：命中 stale/gap 时产出事件并按通知策略出站（复用 alerts._notify）

诚实边界：只能重跑「采集型」监控；第一方推送（OpenAPI/文件导入）没有回源能力，
只能告警提示重推（状态标为 `push_required`）。
"""

from datetime import UTC, datetime, timedelta

DEFAULT_SLA_HOURS = 26.0          # 日更指标默认 SLA（比 24h 留一次重试余量）
DEFAULT_WINDOW_DAYS = 14


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


async def check_workspace(workspace_id: str, *, window_days: int = DEFAULT_WINDOW_DAYS,
                          sla_hours: float | None = None,
                          staleness_only: bool = False) -> dict:
    """体检：返回 {summary, items[], sla, window_days, checked_at}"""
    from ..core.store import get_store
    store = await get_store()
    ws = await store.get_workspace(workspace_id)
    settings = (ws.settings_json if ws else {}) or {}
    sla_map = dict(settings.get("dq_sla") or {})
    now = datetime.now(UTC)
    since = (now - timedelta(days=window_days)).isoformat()
    # 窗口内统计 + 全量「最近一次」（停更指标可能整窗无数据，必须靠全量查询才能发现）
    rows = await store._fetchall(
        """SELECT metric, COUNT(*) AS n, MAX(ts) AS last_ts, MIN(ts) AS first_ts,
                  COUNT(DISTINCT substr(ts, 1, 10)) AS days
           FROM metrics WHERE workspace_id = ? AND ts >= ?
           GROUP BY metric ORDER BY last_ts ASC""", (workspace_id, since))
    all_last = await store._fetchall(
        """SELECT metric, MAX(ts) AS last_ts FROM metrics
           WHERE workspace_id = ? GROUP BY metric""", (workspace_id,))
    seen = {str(r["metric"]) for r in rows}
    for r in all_last:
        if str(r["metric"]) not in seen:            # 整窗无数据 → 停更候选
            rows.append({"metric": r["metric"], "n": 0, "last_ts": r["last_ts"],
                         "first_ts": r["last_ts"], "days": 0})
    monitors = await store.list_monitors_full(workspace_id)
    items = []
    for r in rows:
        metric = str(r["metric"])
        last = _parse(r.get("last_ts"))
        age_h = round((now - last).total_seconds() / 3600, 1) if last else None
        sla = float(sla_map.get(metric, sla_hours or DEFAULT_SLA_HOURS))
        if age_h is None:
            status = "missing"
        elif age_h > sla * 2:
            status = "stale"
        elif age_h > sla:
            status = "late"
        else:
            status = "fresh"
        points = int(r.get("n") or 0)
        days = int(r.get("days") or 0)
        gaps = (await detect_gaps(workspace_id, metric, window_days)
                if points else [])
        item = {"metric": metric, "status": status, "age_hours": age_h,
                "sla_hours": sla, "last_ts": str(r.get("last_ts") or ""),
                "points": points, "covered_days": days,
                "expected_days": window_days, "gaps": gaps[:20],
                "gap_count": len(gaps),
                "recoverable": "monitor" if _monitor_for(monitors, metric) else "push_required"}
        items.append(item)
    summary = {
        "metrics": len(items),
        "fresh": sum(1 for i in items if i["status"] == "fresh"),
        "late": sum(1 for i in items if i["status"] == "late"),
        "stale": sum(1 for i in items if i["status"] == "stale"),
        "missing": sum(1 for i in items if i["status"] == "missing"),
        "with_gaps": sum(1 for i in items if i["gap_count"] > 0),
        "recoverable": sum(1 for i in items if i["status"] != "fresh"
                           and i["recoverable"] == "monitor"),
    }
    out = {"summary": summary, "items": items if not staleness_only else
           [i for i in items if i["status"] != "fresh"],
           "sla": {"default_hours": sla_hours or DEFAULT_SLA_HOURS, "overrides": sla_map},
           "window_days": window_days, "checked_at": now.isoformat()}
    return out


# 监控类型 → 产出指标（用于判断能否自动续采）
KIND_METRICS = {
    "keyword": ["gsc_impressions", "gsc_clicks", "gsc_ctr", "gsc_position"],
    "site_change": ["site_change_score"],
    "brand_mention": ["topic_mentions"],
    "topic": ["topic_negative_ratio", "topic_mentions"],
    "journey": ["journey_step", "ga4_retention"],
}


def _monitor_for(monitors: list, metric: str) -> dict | None:
    """找出能产出该指标的监控任务（用于判断能否自动续采）"""
    for m in monitors or []:
        kind = str((m or {}).get("kind") or "")
        if metric in KIND_METRICS.get(kind, []):
            return {"id": str((m or {}).get("id") or ""), "kind": kind}
    return None


async def detect_gaps(workspace_id: str, metric: str, days: int = 14,
                      *, known_days: int | None = None) -> list[str]:
    """缺口日列表（窗口内应有日 vs 实有日）"""
    from ..core.store import get_store
    store = await get_store()
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    rows = await store._fetchall(
        """SELECT DISTINCT substr(ts, 1, 10) AS d FROM metrics
           WHERE workspace_id = ? AND metric = ? AND ts >= ?""",
        (workspace_id, metric, since))
    have = {str(r["d"]) for r in rows}
    today = datetime.now(UTC).date()
    # 只考核「已结束的日」：今天尚未结束，不算缺口（避免每天 00:00~采集前误报）
    expected = [str(today - timedelta(days=d)) for d in range(1, days)]
    return [d for d in sorted(expected) if d not in have]


async def backfill(workspace_id: str, *, days: int = 7, metrics: list[str] | None = None,
                   dry_run: bool = False) -> dict:
    """断点续采：对缺口指标重跑对应监控任务（幂等窗口键保证不重复）

    参数 metrics 为空时自动挑选「有缺口且可续采」的指标。
    """
    from ..core.store import get_store
    report = await check_workspace(workspace_id, window_days=days)
    targets = [i for i in report["items"]
               if i["gap_count"] > 0 and (not metrics or i["metric"] in metrics)]
    store = await get_store()
    monitors = await store.list_monitors_full(workspace_id)
    planned, skipped, ran = [], [], []
    for item in targets:
        mon = _monitor_for(monitors, item["metric"])
        if not mon:
            skipped.append({"metric": item["metric"], "reason": "push_required"})
            continue
        planned.append({"metric": item["metric"], "monitor_id": mon["id"],
                        "gaps": item["gaps"][:10]})
    if dry_run or not planned:
        return {"planned": planned, "skipped": skipped, "ran": ran, "dry_run": dry_run}
    from ..core.scheduler import get_scheduler
    from .monitors import MonitorService
    svc = MonitorService(workspace_id, scheduler=get_scheduler("default"))
    for p in planned:
        try:
            result = await svc.run(p["monitor_id"])
            ran.append({"metric": p["metric"], "monitor_id": p["monitor_id"],
                        "result": result})
        except Exception as e:               # 单个失败不影响其它
            skipped.append({"metric": p["metric"], "reason": f"{type(e).__name__}: {e}"})
    return {"planned": planned, "skipped": skipped, "ran": ran, "dry_run": False}


async def alert_stale(workspace_id: str) -> dict:
    """新鲜度/缺口告警：命中即发通知（供调度器每日调用）"""
    report = await check_workspace(workspace_id)
    bad = [i for i in report["items"] if i["status"] in ("stale", "missing")]
    gappy = [i for i in report["items"] if i["status"] != "fresh" and i["gap_count"] > 0]
    if not bad and not gappy:
        return {"fired": 0}
    from ..core.files import EventBus
    from .alerts import _notify
    names = "、".join(f"{i['metric']}({i['status']}, {i['age_hours']}h)"
                     for i in bad[:6]) or "—"
    gaps = "、".join(f"{i['metric']} 缺 {i['gap_count']} 天" for i in gappy[:6]) or "—"
    await _notify(workspace_id, "数据质量告警：数据停更/缺口",
                  f"停更或缺失：{names}；缺口：{gaps}；可在运维舱一键续采",
                  ["feishu", "webhook", "email"], {})
    EventBus(workspace_id).emit("dq.alert", {"stale": len(bad), "gaps": len(gappy)})
    return {"fired": len(bad) + len(gappy), "stale": bad[:6], "gaps": gappy[:6]}
