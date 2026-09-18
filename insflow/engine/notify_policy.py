"""通知策略（对标 Grafana notification policies 的"够用版"）

三段策略，全部可选、fail-open（缺省=立即通知，不改变既有行为）：
1. **静默时段** `quiet_hours`: {"start":"22:00","end":"08:00","tz_offset":8,"weekend":true}
   —— 命中则不发，转为"待发队列"（次日恢复时补发，避免漏）
2. **节流聚合** `grouping`: {"group_by":"metric|rule|none","min_interval_s":900}
   —— 同组窗口内合并为一条（附"N 条相似"），避免告警风暴
3. **升级链** `escalation_chain`: [{"after_minutes":0,"to":["webhook"]},
   {"after_minutes":60,"to":["feishu"]}] —— 逐级在超时后升级

状态用工作区设置 `notify_policy` + 内存节流表（单实例；多实例需 Redis，见 docs/11）。
"""

import time
from datetime import UTC, datetime, timedelta

MAX_PENDING = 200


class QuietHours:
    def __init__(self, cfg: dict | None):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("start") and cfg.get("end"))
        self.start = str(cfg.get("start", "22:00"))
        self.end = str(cfg.get("end", "08:00"))
        self.tz_offset = float(cfg.get("tz_offset", 8))     # 默认东八区
        self.weekend = bool(cfg.get("weekend", False))

    def _minutes(self, hhmm: str) -> int:
        try:
            h, m = str(hhmm).split(":")
            return int(h) * 60 + int(m)
        except (ValueError, TypeError):
            return 0

    def active(self, now: datetime | None = None) -> bool:
        if not self.enabled:
            return False
        local = (now or datetime.now(UTC)) + timedelta(hours=self.tz_offset)
        if self.weekend and local.weekday() >= 5:
            return True
        cur = local.hour * 60 + local.minute
        start, end = self._minutes(self.start), self._minutes(self.end)
        if start == end:
            return False
        if start < end:                       # 同日区间
            return start <= cur < end
        return cur >= start or cur < end      # 跨日区间（如 22:00→08:00）

    def next_resume(self, now: datetime | None = None) -> str:
        """静默结束时间（用于"将于何时补发"）"""
        base = (now or datetime.now(UTC)) + timedelta(hours=self.tz_offset)
        end_m = self._minutes(self.end)
        candidate = base.replace(hour=0, minute=0, second=0, microsecond=0) + \
            timedelta(minutes=end_m)
        if candidate <= base:
            candidate += timedelta(days=1)
        return (candidate - timedelta(hours=self.tz_offset)).isoformat()


class Throttle:
    """组内节流（内存态）：同组在 min_interval 内只发一次，其余计入 suppressed"""

    def __init__(self):
        self._last: dict[str, float] = {}
        self._count: dict[str, int] = {}

    def check(self, group: str, min_interval_s: float) -> tuple[bool, int]:
        now = time.time()
        last = self._last.get(group, 0.0)
        if min_interval_s > 0 and now - last < min_interval_s:
            self._count[group] = self._count.get(group, 0) + 1
            return False, self._count[group]
        suppressed = self._count.pop(group, 0)
        self._last[group] = now
        return True, suppressed

    def reset(self) -> None:
        self._last.clear()
        self._count.clear()


throttle = Throttle()
_pending: list[dict] = []


def policy_of(settings: dict | None) -> dict:
    return dict((settings or {}).get("notify_policy") or {})


def group_key(alert: dict, policy: dict) -> str:
    mode = str(policy.get("group_by") or "metric")
    if mode == "none":
        return "-"
    if mode == "rule":
        return str(alert.get("rule_id") or alert.get("name") or "-")
    return str(alert.get("metric") or "-")


def defer(alert: dict, reason: str) -> None:
    """静默/节流期间入待发队列（上限保护，避免内存膨胀）"""
    _pending.append({"alert": alert, "reason": reason, "at": time.time()})
    if len(_pending) > MAX_PENDING:
        del _pending[:len(_pending) - MAX_PENDING]


def peek_pending(workspace_id: str = "") -> list[dict]:
    """查看待发（不取出）；可按工作区过滤"""
    if not workspace_id:
        return list(_pending)
    return [i for i in _pending
            if (i["alert"].get("workspace_id") or workspace_id) == workspace_id]


def drain_pending(workspace_id: str = "") -> list[dict]:
    """取出待发（静默结束或节流窗口过后调用）；可按工作区过滤"""
    if not workspace_id:
        out, _pending[:] = list(_pending), []
        return out
    out = [i for i in _pending
           if (i["alert"].get("workspace_id") or workspace_id) == workspace_id]
    _pending[:] = [i for i in _pending if i not in out]
    return out


def pending_count() -> int:
    return len(_pending)


def escalation_targets(chain: list | None, elapsed_minutes: float = 0.0) -> list[str]:
    """按已过分钟数取应通知的通道（升级链逐级累加）"""
    if not chain:
        return []
    out: list[str] = []
    for step in chain:
        try:
            after = float(step.get("after_minutes", 0))
        except (TypeError, ValueError):
            after = 0
        if elapsed_minutes >= after:
            to = step.get("to") or []
            out.extend([str(x) for x in (to if isinstance(to, list) else [to])])
    seen, uniq = set(), []
    for ch in out:
        if ch not in seen:
            seen.add(ch)
            uniq.append(ch)
    return uniq
