"""实时流（SSE）与在线协同（presence）

设计取舍（诚实）：
- 用 **SSE**（Server-Sent Events）而非 WebSocket：单向推送足够、零依赖、过反代更稳
  （宝塔/Nginx 只需 `proxy_buffering off`）。注意：多人协同光标是单向广播，也够用。
- 频道：`metrics`（指标快照）/ `presence`（谁在看、光标位置）/ `alerts`（阈值命中）
- 节流：每个连接最小推送间隔 `min_interval`（默认 5s），避免把数据库打满；
  无变化不推送（只发心跳注释行），前端页面隐藏时暂停。

局限：进程内内存态（单实例）。多实例部署需接 Redis pub/sub（docs/11 已有 Redis 规划）。
"""

import asyncio
import json
import time
from collections import deque
from datetime import UTC, datetime, timedelta

PRESENCE_TTL = 45          # 秒：超过视为离线
MAX_PRESENCE = 50          # 单工作区最多跟踪的连接数（防内存膨胀）


class Presence:
    """在线状态与光标（内存态，单实例）"""

    def __init__(self):
        self._by_ws: dict[str, dict[str, dict]] = {}

    def _bucket(self, workspace_id: str) -> dict[str, dict]:
        if workspace_id not in self._by_ws:
            self._by_ws[workspace_id] = {}
        return self._by_ws[workspace_id]

    def touch(self, workspace_id: str, conn_id: str, *, user: str = "",
              path: str = "", cursor: dict | None = None) -> None:
        bucket = self._bucket(workspace_id)
        if len(bucket) >= MAX_PRESENCE and conn_id not in bucket:
            return
        entry = bucket.get(conn_id) or {"joined_at": time.time(), "cursor": None}
        entry.update({"user": user or entry.get("user", "访客"),
                      "path": path or entry.get("path", ""),
                      "last_seen": time.time()})
        if cursor is not None:
            entry["cursor"] = cursor
        bucket[conn_id] = entry

    def leave(self, workspace_id: str, conn_id: str) -> None:
        self._by_ws.get(workspace_id, {}).pop(conn_id, None)

    def snapshot(self, workspace_id: str) -> list[dict]:
        bucket = self._bucket(workspace_id)
        now = time.time()
        for cid in [c for c, e in bucket.items()
                    if now - e.get("last_seen", 0) > PRESENCE_TTL]:
            bucket.pop(cid, None)
        return [{"conn_id": cid, "user": e.get("user", "访客"),
                 "path": e.get("path", ""), "cursor": e.get("cursor"),
                 "idle_s": round(now - e.get("last_seen", now), 1)}
                for cid, e in bucket.items()][:MAX_PRESENCE]


presence = Presence()


def sse_event(event: str, data, *, event_id: str = "") -> str:
    """SSE 帧（含可选 id，便于前端 Last-Event-ID 续传）"""
    payload = json.dumps(data, ensure_ascii=False, default=str)
    head = f"id: {event_id}\n" if event_id else ""
    return f"{head}event: {event}\ndata: {payload}\n\n"


async def metric_snapshot(workspace_id: str, metrics: list[str], days: float = 7,
                          dim_filters: dict | None = None) -> dict:
    """指标快照（KPI 值 + 迷你序列），供实时卡片刷新"""
    from ..core.store import get_store
    store = await get_store()
    out: dict[str, dict] = {}
    for metric in metrics[:8]:
        total = await store.metric_total(workspace_id, metric, days=days,
                                         dim_filters=dim_filters)
        series = await store.metric_series(workspace_id, metric, days=days,
                                          dim_filters=dim_filters, limit=24)
        out[metric] = {"value": total,
                       "spark": [r["value"] for r in series][-24:]}
    return {"at": datetime.now(UTC).isoformat(), "metrics": out}


def recent_alert_events(workspace_id: str, limit: int = 5) -> list[dict]:
    """最近告警事件（从事件流读，用于 SSE alerts 频道补发）"""
    from ..core.files import EventBus
    events = EventBus(workspace_id).read(limit=200)
    return [e for e in events if str(e.get("type", "")).startswith("alert.")][-limit:]


class StreamHub:
    """把"按间隔采样"变成异步生成器（可测试，不依赖真实 HTTP 连接）"""

    def __init__(self, workspace_id: str, *, metrics: list[str], days: float = 7,
                 dim_filters: dict | None = None, interval: float = 5.0,
                 max_ticks: int | None = None, heartbeat: float = 15.0):
        self.workspace_id = workspace_id
        self.metrics = metrics
        self.days = days
        self.dim_filters = dim_filters
        self.interval = max(2.0, float(interval))
        self.heartbeat = max(self.interval, float(heartbeat))
        self.max_ticks = max_ticks
        self._last = None

    async def events(self):
        """产出 (event, data) 序列；无变化只发心跳"""
        ticks = 0
        seen_alerts: deque = deque(maxlen=50)
        last_beat = time.time()
        while self.max_ticks is None or ticks < self.max_ticks:
            ticks += 1
            snap = await metric_snapshot(self.workspace_id, self.metrics, self.days,
                                        self.dim_filters)
            changed = (self._last is None or
                       any(abs(snap["metrics"].get(k, {}).get("value", 0) -
                               (self._last or {}).get(k, {}).get("value", 0)) > 1e-9
                           for k in self.metrics))
            if changed:
                self._last = snap["metrics"]
                yield "metrics", snap
            for ev in recent_alert_events(self.workspace_id):
                key = f"{ev.get('ts')}:{ev.get('type')}"
                if key not in seen_alerts:
                    seen_alerts.append(key)
                    yield "alert", ev
            now = time.time()
            if now - last_beat >= self.heartbeat:
                last_beat = now
                yield "heartbeat", {"at": datetime.now(UTC).isoformat()}
            if self.max_ticks is not None and ticks >= self.max_ticks:
                break
            await asyncio.sleep(self.interval)
        yield "done", {"ticks": ticks}


def parse_metrics(raw: str, limit: int = 8) -> list[str]:
    """查询参数 → 指标白名单（仅允许词法安全的指标名）"""
    import re
    names = [m.strip() for m in (raw or "").split(",") if m.strip()]
    safe = [n for n in names if re.fullmatch(r"[a-zA-Z0-9_:.]{1,64}", n)]
    return safe[:limit]


def since_from_days(days: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()
