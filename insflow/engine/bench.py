"""性能基准（性能守则的可验证部分）

对标 docs/07 的性能基线：单实例 1000+ 监控任务、洞察流 100k+ 行的查询延迟。
一次性写入合成数据 → 跑关键查询 → 输出 P50/P95（不依赖外部压测工具）。

用法：`insflow bench --monitors 1000 --insights 100000 --metrics 200000`
提示：默认用临时 SQLite（不影响生产库）；`--driver mysql` 需配置 .env。
"""

import asyncio
import os
import statistics
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .demo import generate_id


class _Timer:
    def __init__(self):
        self.samples: dict[str, list[float]] = {}

    def record(self, name: str, ms: float) -> None:
        self.samples.setdefault(name, []).append(ms)

    async def run(self, name: str, coro) -> None:
        t0 = time.perf_counter()
        await coro
        self.record(name, (time.perf_counter() - t0) * 1000)

    def report(self) -> dict:
        out = {}
        for name, xs in self.samples.items():
            ordered = sorted(xs)
            out[name] = {
                "n": len(xs),
                "p50_ms": round(statistics.median(ordered), 2),
                "p95_ms": round(ordered[max(0, int(len(ordered) * 0.95) - 1)], 2),
                "max_ms": round(ordered[-1], 2),
            }
        return out


async def run_bench(*, monitors: int = 1000, insights: int = 100_000,
                    metrics: int = 200_000, driver: str = "sqlite",
                    workspace_id: str = "bench", keep: bool = False,
                    rounds: int = 5) -> dict:
    """写入合成数据并测关键查询（返回报告 dict）"""
    from ..core.store import Store, reset_store
    from ..viz import charts as _c  # noqa: F401  预热导入

    tmp = Path(tempfile.mkdtemp(prefix="insflow-bench-"))
    if driver == "mysql":
        store = Store()
    else:
        os.environ.setdefault("INSFLOW_DB_DRIVER", "sqlite")
        store = Store(db_path=tmp / "bench.db")
    await store.connect()
    await store.migrate()
    reset_store(store)

    from ..core.entities import Workspace
    await store.create_workspace(Workspace(id=workspace_id, name="Benchmark"))

    timer = _Timer()
    now = datetime.now(UTC)

    # ---- 造数（批量事务，避免逐行 fsync）----
    t0 = time.perf_counter()
    await store._execute("BEGIN")
    for i in range(monitors):
        await store._execute(
            """INSERT INTO monitors (id, workspace_id, kind, target_json,
               schedule_cron, state, created_at)
               VALUES (?, ?, 'keyword', ?, '0 * * * *', 'idle', ?)""",
            (generate_id(), workspace_id,
             f'{{"demo": true, "name": "monitor-{i}"}}', now.isoformat()))
    for i in range(insights):
        await store._execute(
            """INSERT INTO insights (id, workspace_id, type, title, summary, severity,
               confidence, status, evidence_json, models_json, actions_json,
               stage_tags_json, created_at)
               VALUES (?, ?, 'bench_insight', ?, 'bench', 'medium', 0.5, 'new',
                       '[{"demo": true}]', '[]', '[]', '[]', ?)""",
            (generate_id(), workspace_id, f"insight-{i}", now.isoformat()))
    for i in range(metrics):
        await store._execute(
            """INSERT INTO metrics (id, workspace_id, entity_type, entity_id, metric,
               value, dim_json, ts, monitor_id, window_key)
               VALUES (?, ?, 'site', 'main', 'ga4_sessions', ?, '{"demo": true}', ?, 'bench', ?)""",
            (generate_id(), workspace_id, float(i % 1000),
             (now - timedelta(minutes=i % 43200)).isoformat(),
             f"w{i}"))
    await store._db.commit()
    write_s = time.perf_counter() - t0

    # ---- 查询（含冷/热：先跑一轮预热，再取多轮 P50/P95）----
    for _ in range(rounds):
        await timer.run("insights.list(200)",
                        store.list_insights(workspace_id, limit=200))
        await timer.run("insights.filter(status+severity)",
                        store.list_insights(workspace_id, status="new",
                                            severity="medium", limit=200))
        await timer.run("insights.page(offset=5000)",
                        store.list_insights(workspace_id, limit=200, offset=5000))
        await timer.run("metrics.series(30d)",
                        store.metric_series(workspace_id, "ga4_sessions", days=30))
        await timer.run("metrics.total(7d)",
                        store.metric_total(workspace_id, "ga4_sessions", days=7))
        await timer.run("metrics.catalog(90d)",
                        store.metric_catalog(workspace_id, days=90))
        await timer.run("metrics.breakdown(entity)",
                        store.metric_breakdown(workspace_id, "ga4_sessions", days=30))
        await timer.run("admin_audit.list(200)",
                        store.list_admin_audit(workspace_id, limit=200))

    # ---- 预聚合后再测长窗口（性能守则：≥30 天走汇总表）----
    t_roll = time.perf_counter()
    from ..core.rollup import rollup
    rolled = await rollup(workspace_id, days=400)
    roll_s = time.perf_counter() - t_roll
    for _ in range(rounds):
        await timer.run("metrics.series(30d, rollup)",
                        store.metric_series(workspace_id, "ga4_sessions", days=30))

    report = {
        "driver": store.driver,
        "rollup": {"rows": rolled.get("rows", 0), "seconds": round(roll_s, 2)},
        "dataset": {"monitors": monitors, "insights": insights, "metrics": metrics},
        "write": {"seconds": round(write_s, 2),
                  "rows_per_sec": round((monitors + insights + metrics) / max(write_s, 1e-6))},
        "queries": timer.report(),
        "db_file_mb": round((tmp / "bench.db").stat().st_size / 1e6, 1)
        if store.driver == "sqlite" and (tmp / "bench.db").exists() else None,
    }
    await store.close()
    reset_store(None)
    if not keep and driver != "mysql":
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    return report


def run_bench_sync(**kw) -> dict:
    return asyncio.get_event_loop().run_until_complete(run_bench(**kw))
