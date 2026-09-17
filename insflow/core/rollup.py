"""预聚合（物化视图等价物）：metric_daily 汇总 + 长窗口查询回退

动机：驾驶舱默认窗口 7–90 天，直接扫 metrics 明细在数据量大后变慢。
做法：把明细按天汇总进 metric_daily（逐 workspace/metric/entity/dim 分组），
     趋势查询优先读汇总表；命中不了（窗口过短/刚入库未汇总）自动回退明细。
诚实标注：这不是数据库物化视图（MySQL 5.7 无），是应用层汇总表 + 幂等 upsert。
"""

from datetime import UTC, datetime, timedelta

from .store import get_store

MIN_ROLLUP_DAYS = 30          # 窗口 ≥ 该天数才走汇总表（短窗口明细更准更快）


async def rollup(workspace_id: str = "", days: float = 400) -> dict:
    """把明细汇总进 metric_daily（幂等：同键覆盖，可重复执行）"""
    store = await get_store()
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    day_expr = ("DATE(ts)" if store.driver == "mysql" else "substr(ts, 1, 10)")
    dim_expr = (store._backend.dialect.json_field("dim_json", "province")
                if store._backend else "json_extract(dim_json, '$.province')")
    where = "ts >= ?"
    params: list = [since]
    if workspace_id:
        where += " AND workspace_id = ?"
        params.append(workspace_id)
    rows = await store._fetchall(
        f"""SELECT workspace_id, metric, {day_expr} AS day, entity_type, entity_id,
                   COALESCE({dim_expr}, '') AS dim_key,
                   SUM(value) AS agg_sum, AVG(value) AS agg_avg,
                   MAX(value) AS agg_max, COUNT(*) AS n
            FROM metrics WHERE {where}
            GROUP BY workspace_id, metric, day, entity_type, entity_id, dim_key""",
        tuple(params),
    )
    now = datetime.now(UTC).isoformat()
    written = 0
    for r in rows:
        key = (f"{r['workspace_id']}|{r['metric']}|{r['day']}|{r['entity_type']}|"
               f"{r['entity_id']}|{r['dim_key']}")
        import hashlib
        rid = hashlib.sha1(key.encode()).hexdigest()[:24]
        # MySQL 无 INSERT OR REPLACE 语法（用 REPLACE INTO）；SQLite 用 INSERT OR REPLACE
        verb = ("REPLACE INTO" if store.driver == "mysql" else "INSERT OR REPLACE INTO")
        await store._execute(
            f"""{verb} metric_daily
               (id, workspace_id, metric, day, entity_type, entity_id, dim_key,
                agg_sum, agg_avg, agg_max, n, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (rid, r["workspace_id"], r["metric"], str(r["day"]), r["entity_type"],
             r["entity_id"], r["dim_key"], float(r["agg_sum"] or 0),
             float(r["agg_avg"] or 0), float(r["agg_max"] or 0),
             int(r["n"] or 0), now))
        written += 1
    await store._db.commit()
    return {"rows": written, "days": days, "workspace_id": workspace_id}


async def daily_series(workspace_id: str, metric: str, days: float = 90,
                       agg: str = "sum", dim_filters: dict | None = None) -> list[dict]:
    """从汇总表取日粒度序列（dim 只支持 province 维的 dim_key）"""
    store = await get_store()
    since = (datetime.now(UTC) - timedelta(days=days)).date().isoformat()
    fn = {"sum": "SUM(agg_sum)", "avg": "AVG(agg_avg)", "max": "MAX(agg_max)"}.get(
        agg, "SUM(agg_sum)")
    where = "workspace_id = ? AND metric = ? AND day >= ?"
    params: list = [workspace_id, metric, since]
    dim_filters = dim_filters or {}
    if dim_filters.get("province"):
        where += " AND dim_key = ?"
        params.append(str(dim_filters["province"])[:80])
    rows = await store._fetchall(
        f"""SELECT day, {fn} AS v, SUM(n) AS n FROM metric_daily
            WHERE {where} GROUP BY day ORDER BY day""", tuple(params))
    return [{"bucket": r["day"], "value": float(r["v"] or 0), "n": int(r["n"] or 0)}
            for r in rows]


async def rollup_stats(workspace_id: str = "") -> dict:
    """汇总表健康度（行数 / 覆盖窗口 / 最新天数），运维可见"""
    store = await get_store()
    where, params = ("", ()) if not workspace_id else (" WHERE workspace_id = ?",
                                                       (workspace_id,))
    row = await store._fetchone(
        f"""SELECT COUNT(*) AS rows_n, MIN(day) AS first_day, MAX(day) AS last_day,
                   COUNT(DISTINCT metric) AS metrics_n FROM metric_daily{where}""",
        params)
    return {"rows": int((row or {}).get("rows_n") or 0),
            "first_day": (row or {}).get("first_day") or "",
            "last_day": (row or {}).get("last_day") or "",
            "metrics": int((row or {}).get("metrics_n") or 0)}
