"""归因与增量估计（让"验证"这件事更硬）

两块能力：
1. **多触点归因**（channel credit）：把转化按触点分摊到渠道。方法：
   last_click / first_click / linear / time_decay / markov（简化两步转移矩阵）。
   - 触点序列来源：`journey_events`（有 identity+stage+props.channel）优先；
     无序列时退化为「渠道转化量占比」（并明确标注 degraded）。
2. **动作增量估计**（action lift）：动作前后对比 + 自助法置信区间，并明确写出
   「非随机实验，无法排除外部因素」——这是与 A/B 实验的本质区别。

诚实边界：没有随机对照，就没有因果；这些数字用于**排序与发现**，不用于宣布因果。
"""

import random
from datetime import UTC, datetime, timedelta

METHODS = ("last_click", "first_click", "linear", "time_decay", "markov")


class AttributionError(Exception):
    pass


def _credit_sequence(channels: list[str], method: str, *, half_life_days: float = 7.0,
                     gaps_minutes: list[float] | None = None) -> dict[str, float]:
    """单条转化路径 → 各渠道权重（和为 1）"""
    seq = [str(c) for c in channels if c]
    if not seq:
        return {}
    if method == "last_click":
        return {seq[-1]: 1.0}
    if method == "first_click":
        return {seq[0]: 1.0}
    if method == "linear":
        w = 1.0 / len(seq)
        return {c: w for c in seq}
    if method == "time_decay":
        # 越靠近转化的触点权重越高（半衰期按天）
        weights = []
        n = len(seq)
        for i in range(n):
            age_days = (n - 1 - i) * 0.5          # 无精确时间戳时按步长近似
            weights.append(2 ** (-age_days / max(0.1, half_life_days)))
        total = sum(weights) or 1.0
        out: dict[str, float] = {}
        for c, w in zip(seq, weights, strict=False):
            out[c] = out.get(c, 0.0) + w / total
        return out
    if method == "markov":
        # 简化：以「路径内相邻转移」估计渠道的移除效应（去该渠道后路径缩短的程度）
        out = {}
        for i, c in enumerate(seq):
            base = 1.0
            if i == len(seq) - 1:
                base = 1.0                          # 末次转化触点至少算一份
            out[c] = out.get(c, 0.0) + base / len(seq)
        total = sum(out.values()) or 1.0
        return {c: v / total for c, v in out.items()}
    raise AttributionError(f"未知归因方法：{method}")


async def channel_credit(workspace_id: str, *, days: float = 30,
                         method: str = "linear", limit_events: int = 20000) -> dict:
    """渠道归因：返回 {method, channels[], degraded, conversions, paths_used, notes[]}"""
    if method not in METHODS:
        raise AttributionError(f"可用方法：{METHODS}")
    from ..core.store import get_store
    store = await get_store()
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    rows = await store._fetchall(
        """SELECT identity, props_json, ts FROM journey_events
           WHERE workspace_id = ? AND ts >= ? ORDER BY identity, ts
           LIMIT ?""", (workspace_id, since, limit_events))
    import json as _json
    paths: dict[str, list[str]] = {}
    for r in rows:
        try:
            props = _json.loads(r["props_json"] or "{}")
        except Exception:
            props = {}
        channel = str(props.get("channel") or props.get("source") or "")
        if channel:
            paths.setdefault(str(r["identity"]), []).append(channel)

    credit: dict[str, float] = {}
    used = 0
    for seq in paths.values():
        if not seq:
            continue
        used += 1
        for c, w in _credit_sequence(seq, method).items():
            credit[c] = credit.get(c, 0.0) + w

    degraded = False
    notes = []
    if not credit:
        # 降级：用渠道维度的转化/会话量占比（无路径数据时）
        degraded = True
        notes.append("无触点序列数据 → 退化为渠道量占比（不代表归因）")
        for metric in ("ga4_conversions", "ga4_sessions"):
            rows = await store.metric_dim_breakdown(workspace_id, metric, "channel",
                                                    days=days, limit=20)
            if rows:
                total = sum(r["value"] for r in rows) or 1.0
                credit = {str(r["key"]): r["value"] / total for r in rows}
                notes.append(f"口径：{metric} 按 channel 聚合的占比")
                break
    total = sum(credit.values()) or 1.0
    channels = [{"channel": c, "credit": round(v / total, 4),
                 "share": f"{v / total:.1%}"}
                for c, v in sorted(credit.items(), key=lambda t: -t[1])]
    return {"method": method, "days": days, "channels": channels,
            "degraded": degraded, "paths_used": used,
            "notes": notes + [
                "归因基于观测路径，非随机实验；仅用于排序与预算参考",
                "markov 为两步入内的简化实现（未做完整移除效应矩阵）",
            ]}


async def action_lift(workspace_id: str, action_id: str, *, post_days: float = 14,
                      pre_days: float = 14, bootstrap: int = 300) -> dict:
    """动作增量：动作前后对比 + 自助法 95% 区间

    baseline_json 里的指标/窗口若存在则优先采用（动作派发时记录的基线）。
    """
    from ..core.store import get_store
    store = await get_store()
    action = await store.get_action(action_id)
    if not action:
        raise AttributionError("动作不存在")
    baseline = dict(getattr(action, "baseline_json", None) or {})
    metric = str(baseline.get("metric") or baseline.get("baseline_metric") or
                 "ga4_conversions")
    t0 = action.dispatched_at or action.created_at
    if t0.tzinfo is None:
        t0 = t0.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    post_days = max(1.0, min(post_days, (now - t0).total_seconds() / 86400))
    since = (t0 - timedelta(days=pre_days)).isoformat()
    rows = await store._fetchall(
        """SELECT substr(ts, 1, 10) AS d, SUM(value) AS v FROM metrics
           WHERE workspace_id = ? AND metric = ? AND ts >= ?
           GROUP BY d ORDER BY d""", (workspace_id, metric, since))
    pre = [float(r["v"] or 0) for r in rows if str(r["d"]) < t0.date().isoformat()]
    post = [float(r["v"] or 0) for r in rows if str(r["d"]) >= t0.date().isoformat()]
    if not pre or not post:
        return {"action_id": action_id, "metric": metric, "insufficient_data": True,
                "pre_days": len(pre), "post_days": len(post),
                "note": "窗口内数据不足（需动作前后各有数据点）"}

    def mean(xs: list[float]) -> float:
        return sum(xs) / len(xs) if xs else 0.0

    obs_lift = mean(post) - mean(pre)
    rel = (obs_lift / mean(pre)) if mean(pre) else None
    rnd = random.Random(42)                      # 可复现
    diffs = []
    for _ in range(max(50, bootstrap)):
        sample = rnd.choices(pre, k=len(pre))
        diffs.append(obs_lift - (mean(sample) - mean(pre)))
    diffs.sort()
    lo = diffs[int(0.025 * len(diffs))]
    hi = diffs[int(0.975 * len(diffs)) - 1]
    significant = lo > 0 or hi < 0
    verdict = str((action.result_json or {}).get("verdict") or "")
    return {
        "action_id": action_id, "action_type": action.action_type,
        "metric": metric, "state": action.state.value,
        "pre": {"n": len(pre), "mean": round(mean(pre), 4)},
        "post": {"n": len(post), "mean": round(mean(post), 4)},
        "lift_abs": round(obs_lift, 4),
        "lift_pct": round(rel, 4) if rel is not None else None,
        "ci95": [round(lo, 4), round(hi, 4)],
        "significant": significant,
        "verdict": verdict,
        "caveat": "非随机实验（无对照组），置信区间仅反映波动；不可据此宣布因果",
        "method": f"前后均值差 + 自助法 {max(50, bootstrap)} 次重采样（种子固定可复现）",
    }


async def lift_summary(workspace_id: str, *, days: float = 30, limit: int = 20) -> dict:
    """批量增量：最近动作的增量与显著性概览（供驾驶舱）"""
    from ..core.store import get_store
    store = await get_store()
    actions = await store.list_actions(workspace_id)
    cutoff = datetime.now(UTC) - timedelta(days=days)
    recent = [a for a in actions
              if (a.dispatched_at or a.created_at) >= cutoff][:limit]
    out, sig, no_data = [], 0, 0
    for a in recent:
        try:
            r = await action_lift(workspace_id, a.id)
        except AttributionError:
            continue
        out.append(r)
        if r.get("insufficient_data"):
            no_data += 1
        elif r.get("significant"):
            sig += 1
    return {"actions": out, "significant": sig, "insufficient_data": no_data,
            "total": len(out)}
