"""Insight Flow 频率治理（对齐 OpenFlow 心跳降频教训）

OpenFlow 教训：生产曾积累 63 万行无意义 heartbeat/scroll 事件被全部清空。
对策分两处：
1. **前端**：心跳降频（延迟 60s、间隔 120s、单页上限、隐藏暂停）——见 base.html
2. **后端**（本模块）：
   a. 监控任务 cron **最小间隔守卫**：拒绝过于频繁的调度（省钱 + 少写库）
   b. 事件流**写入降频**：同 (workspace, type, 业务键) 在窗口内重复 → 合并为一条 + count
"""

import json
import os
import time
from datetime import datetime, timezone

from .files import EventBus

# 各监控类型的最小间隔（分钟）——低于此值拒绝创建（避免高频打源/写库）
MIN_INTERVAL_MINUTES = {
    "site_change": 30,      # 网页变更：半小时足够
    "keyword": 360,         # 关键词排名：6 小时（GSC 数据本身滞后数天）
    "brand_mention": 60,    # 品牌提及：1 小时
    "topic": 30,            # 主题舆情：半小时
    "journey": 360,         # 旅程漏斗：6 小时
}
DEFAULT_MIN_INTERVAL = 30

# 事件流降频：这些事件类型在窗口内合并（高频重复无信息量）
DEDUPE_EVENTS = {
    "monitor.run_finished": 300,        # 5 分钟窗口
    "source.collected": 180,
    "quota.warning": 3600,
    "subscription.pushed": 300,
}
DEDUPE_BUSINESS_KEY = {
    "monitor.run_finished": "monitor_id",
    "source.collected": "source",
    "quota.warning": "kind",
    "subscription.pushed": "insight_id",
}


class CronTooFrequent(Exception):
    """调度过于频繁（频率治理）"""
    pass


def parse_cron_minutes(cron: str) -> float | None:
    """解析 cron 的最小间隔（分钟）；无法判断返回 None

    支持标准 5 段：分钟 小时 日 月 星期
    - 分钟段为 */N → N 分钟
    - 分钟段为固定值且小时段为 *  → 60 分钟
    - 小时段为 */N → N*60 分钟
    - 小时段为固定值且日段为 * → 1440 分钟
    """
    if not cron or not isinstance(cron, str):
        return None
    parts = cron.split()
    if len(parts) != 5:
        return None
    minute, hour, dom, month, dow = parts

    def step(expr: str) -> int | None:
        if expr in ("*", "?"):
            return None
        if expr.startswith("*/"):
            try:
                return int(expr[2:])
            except ValueError:
                return None
        return None

    m_step = step(minute)
    if m_step:
        return float(m_step)
    h_step = step(hour)
    if h_step:
        return float(h_step * 60)
    if minute not in ("*", "?") and hour in ("*", "?"):
        return 60.0
    if minute not in ("*", "?") and hour not in ("*", "?"):
        if dom in ("*", "?"):
            return 1440.0
        return 1440.0
    return None


def validate_cron(kind: str, cron: str) -> None:
    """校验监控 cron 频率是否合规（低于最小间隔则拒绝）"""
    minimum = MIN_INTERVAL_MINUTES.get(kind, DEFAULT_MIN_INTERVAL)
    minutes = parse_cron_minutes(cron)
    if minutes is not None and minutes < minimum:
        raise CronTooFrequent(
            f"{kind} 监控最小间隔为 {minimum} 分钟，当前 cron '{cron}' 约每 "
            f"{minutes:.0f} 分钟一次。频率过高会放大数据源成本与写库压力。"
        )


# ================= 事件流写入降频 =================

_RECENT: dict[str, tuple[float, int]] = {}


def emit_throttled(bus: EventBus, event_type: str, payload: dict,
                   business_key: str | None = None) -> bool:
    """带降频的事件写入

    同一 (workspace, event_type, 业务键) 在窗口内重复 → 只记第一条，后续累加 count
    （避免高频重复事件撑爆 events.jsonl；关键事件不入降频白名单，直接写）

    Returns: True=实际写入，False=被合并
    """
    window = DEDUPE_EVENTS.get(event_type)
    if not window:
        bus.emit(event_type, payload)
        return True

    key_field = DEDUPE_BUSINESS_KEY.get(event_type)
    biz = str(payload.get(business_key or key_field, "")) if (business_key or key_field) else ""
    key = f"{bus.workspace_id}:{event_type}:{biz}"

    now = time.time()
    last = _RECENT.get(key)
    if last and now - last[0] < window:
        _RECENT[key] = (last[0], last[1] + 1)
        return False
    _RECENT[key] = (now, 1)
    bus.emit(event_type, payload)
    return True


def dedupe_stats() -> dict:
    """降频统计（运维舱展示）"""
    return {"tracked_keys": len(_RECENT),
            "merged_total": sum(c for _, c in _RECENT.values() if c > 1) - 
                            sum(1 for _, c in _RECENT.values() if c > 1)}
