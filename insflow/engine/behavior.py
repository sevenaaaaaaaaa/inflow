"""行为信号（本地统计，不上传）：谁在看什么 → 建议默认布局与常用面板

为什么要它：洞察是"流"，但人每天真正回看的只有两三块面板。把**浏览/导出/订阅**
计数存在本地 `behavior_signals` 表里，就能把"默认进哪个舱、默认看哪几块面板"
从拍脑袋变成有依据——这也是结构提案（`structure_board`）唯一的数据来源。

隐私边界（写死在代码里）：
- 只记 (工作区, 类型, 键, 次数)，**不记用户身份、不记 IP、不记时间序列明细**
- 只落本地库，不进事件流、不外发；键是页面/面板名这类非个人数据
- 记账失败一律吞掉（`record` 永不抛）：埋点不能把页面打挂
"""

from __future__ import annotations

from datetime import UTC, datetime

KINDS = ("view", "export", "subscribe")
MAX_KEY = 120

# 驾驶舱名 → 自定义看板面板 id（custom_boards.PANEL_CATALOG）
COCKPIT_PANEL = {
    "overview": "overview.all",
    "traffic": "traffic.channels",
    "sentiment": "sentiment.risk",
    "competitor": "competitor.all",
    "journey": "journey.all",
    "action-loop": "action-loop.all",
    "ops": "ops.all",
    "billing": "billing.all",
}


def _clean(value: str) -> str:
    return str(value or "").strip()[:MAX_KEY]


async def record(workspace_id: str, kind: str, key: str, n: int = 1) -> bool:
    """计数 +n（UPSERT）。任何异常都吞掉——埋点永远不该让页面 500。"""
    if kind not in KINDS:
        return False
    key = _clean(key)
    if not workspace_id or not key:
        return False
    try:
        from ..core.store import generate_id, get_store
        store = await get_store()
        now = datetime.now(UTC).isoformat()
        cur = await store._execute(
            "UPDATE behavior_signals SET hits = hits + ?, last_at = ? "
            "WHERE workspace_id = ? AND kind = ? AND signal_key = ?",
            (int(n), now, workspace_id, kind, key))
        if not getattr(cur, "rowcount", 0):
            await store._execute(
                "INSERT INTO behavior_signals (id, workspace_id, kind, signal_key, "
                "hits, first_at, last_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (generate_id(), workspace_id, kind, key, int(n), now, now))
        await store._db.commit()
        return True
    except Exception:  # noqa: BLE001 —— 见模块注释：埋点 fail-open
        return False


async def top(workspace_id: str, kind: str = "", limit: int = 10) -> list[dict]:
    from ..core.store import get_store
    store = await get_store()
    sql = ("SELECT kind, signal_key, hits, first_at, last_at FROM behavior_signals "
           "WHERE workspace_id = ?")
    params: list = [workspace_id]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " ORDER BY hits DESC, signal_key ASC LIMIT ?"
    params.append(max(1, min(100, limit)))
    return [{"kind": r["kind"], "key": r["signal_key"], "hits": int(r["hits"] or 0),
             "first_at": r["first_at"], "last_at": r["last_at"]}
            for r in await store._fetchall(sql, tuple(params))]


async def summary(workspace_id: str) -> dict:
    rows = await top(workspace_id, limit=100)
    out: dict[str, list] = {k: [] for k in KINDS}
    for r in rows:
        out.setdefault(r["kind"], []).append(r)
    return {"total_events": sum(r["hits"] for r in rows),
            "by_kind": {k: v[:10] for k, v in out.items()}}


async def suggest_defaults(workspace_id: str, *, min_views: int = 3) -> dict:
    """按浏览/导出计数给"默认舱 + 常用面板 + 常用指标"建议

    样本不足就明说 `enough=False` 并返回空建议——不编默认值。
    """
    views = [r for r in await top(workspace_id, "view", limit=30)
             if not r["key"].startswith("board:")]
    exports = await top(workspace_id, "export", limit=10)
    subs = await top(workspace_id, "subscribe", limit=10)
    total = sum(r["hits"] for r in views)
    ranked = [r for r in views if r["hits"] >= min_views]
    panels: list[str] = []
    for r in ranked:
        panel = COCKPIT_PANEL.get(r["key"])
        if panel and panel not in panels:
            panels.append(panel)
    return {
        "enough": bool(ranked),
        "min_views": min_views,
        "total_views": total,
        "default_cockpit": ranked[0]["key"] if ranked else "",
        "panel_ids": panels[:6],
        "top_views": views[:5],
        "top_exports": exports[:5],
        "top_subscriptions": subs[:5],
        "basis": (f"近期浏览 {total} 次，命中 {len(ranked)} 个高频舱（阈值 {min_views} 次）"
                  if ranked else f"浏览样本不足（{total} 次，阈值 {min_views} 次/舱）"),
    }
