"""竞品流量估算（对标 Similarweb 的"估算"能力，但明确方法与置信度）

诚实定位：Similarweb/Semrush 用面板数据（ISP/浏览器/工具栏）+ 模型估算，误差常在
±30%~±60%（中小站点更大）。我们没有面板数据，因此**不做"精确流量"承诺**，
而是把可得信号（外链/引荐域、SERP 出现、内容产出、社媒互动）合成为一个
**相对指数**，并给出方法与置信度，让用户知道该数字能用来比较趋势、不能当绝对值用。

输出：{"index": 0~100, "range": [lo, hi], "confidence": "low|medium", "method": [...], "signals": {...}}
"""

from datetime import UTC, datetime, timedelta

from ..core.store import get_store

SOURCE_WEIGHTS = {
    "referring_domains": 0.34,   # 引荐域数量（外链广度的近似）
    "serp_presence": 0.30,       # SERP 出现次数/关键词覆盖
    "content_output": 0.20,      # 内容产出频率（站点活跃度）
    "social_engagement": 0.16,   # 社媒互动（传播面）
}


def _norm(value: float, scale: float) -> float:
    """平方根归一（0~1）：比对数更能拉开差距，同时不放大头部（估算只看相对量级）"""
    if value <= 0:
        return 0.0
    return min(1.0, (value / max(scale, 1e-6)) ** 0.5)


async def estimate_domain(workspace_id: str, domain: str, days: float = 30) -> dict:
    """估算某域名的相对流量指数（0~100，仅用于横向比较与趋势）"""
    store = await get_store()
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    rows = await store._fetchall(
        """SELECT metric, value FROM metrics
           WHERE workspace_id = ? AND entity_id = ? AND ts >= ?""",
        (workspace_id, domain, since))
    agg: dict[str, float] = {}
    for r in rows:
        agg[r["metric"]] = agg.get(r["metric"], 0.0) + float(r["value"] or 0)

    signals = {
        "referring_domains": agg.get("referring_domains", 0.0),
        "serp_presence": agg.get("serp_organic", 0.0) + agg.get("gsc_impressions", 0.0),
        "content_output": agg.get("content_published", 0.0) + agg.get("blog_posts", 0.0),
        "social_engagement": agg.get("social_engagement", 0.0)
                             + agg.get("topic_mentions", 0.0),
    }
    # 归一化到"日均"再比：窗口长度不同也能横向比较（尺度为日均参考量级）
    daily = {k: v / max(1.0, days) for k, v in signals.items()}
    scales = {"referring_domains": 180, "serp_presence": 4200,
              "content_output": 26, "social_engagement": 380}
    score = sum(_norm(daily[k], scales[k]) * SOURCE_WEIGHTS[k] for k in SOURCE_WEIGHTS)
    index = round(score * 100, 1)
    covered = sum(1 for v in signals.values() if v > 0)
    confidence = "low" if covered <= 1 else "medium" if covered <= 2 else "high"
    spread = 0.45 if confidence == "low" else 0.30 if confidence == "medium" else 0.20
    lo, hi = round(index * (1 - spread), 1), round(index * (1 + spread), 1)
    return {
        "domain": domain, "index": index, "range": [lo, hi],
        "confidence": confidence, "signals": signals,
        "method": [
            "日均信号做平方根归一后加权（外链 0.34 / SERP 0.30 / 内容 0.20 / 社媒 0.16）",
            f"置信区间按数据覆盖面取 ±{int(spread * 100)}%",
            "无面板数据，不做绝对流量承诺；仅可用于同口径横向比较与趋势判断",
        ],
        "coverage": f"{covered}/4 类信号有数据",
        "daily_signals": {k: round(v, 2) for k, v in daily.items()},
    }


async def estimate_many(workspace_id: str, domains: list[str], days: float = 30) -> list[dict]:
    return [await estimate_domain(workspace_id, d, days) for d in domains if d]
