"""分布式 leader 选举（定时任务单实例执行）

问题：多实例部署时，定时任务（采集/汇总/告警/周报）会被每个实例各跑一遍 →
重复采集、重复通知、配额浪费。

做法：Redis `SET key value NX PX ttl` 抢锁 + 周期续租；只有 leader 执行任务。
无 Redis（单机私有化）时自动降级为「永远是 leader」，行为与现在完全一致。

注意：**这不是强一致选主**（Redis 主从切换窗口内可能短暂双主），
但配合任务的幂等窗口键，重复执行不会造成数据重复。
"""

import os
import socket
import time

LEADER_KEY = "insflow:leader"
DEFAULT_TTL_MS = 30_000           # 锁 30s
RENEW_INTERVAL = 10.0             # 每 10s 续租


def instance_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


class Leader:
    """leader 锁（Redis 无则单机降级）"""

    def __init__(self, key: str = LEADER_KEY, ttl_ms: int = DEFAULT_TTL_MS,
                 ident: str = ""):
        self.key = key
        self.ttl_ms = ttl_ms
        self.ident = ident or instance_id()
        self.is_leader = False
        self._last_acquire = 0.0

    async def _client(self):
        from .db.redis_backend import get_redis
        return await get_redis()

    async def acquire(self, force: bool = False) -> bool:
        """抢锁/续租；返回当前是否为 leader

        - 无 Redis：恒为 True（单机模式）
        - 已持有：续租（TTL 重置）
        - 未持有：SET NX PX 抢锁；若锁的持有者是自己（重启后残留）也认领
        """
        client = await self._client()
        if client is None:
            self.is_leader = True
            return True
        now = time.time()
        if self.is_leader and not force and now - self._last_acquire < RENEW_INTERVAL:
            return True
        try:
            if self.is_leader:
                # 续租前确认锁仍属于自己（避免续了别人的锁）
                cur = await client.get(self.key)
                if cur not in (None, self.ident):
                    self.is_leader = False
                    return False
                await client.set(self.key, self.ident, px=self.ttl_ms)
                await client.expire(self.key, max(1, self.ttl_ms // 1000))
                self._last_acquire = now
                return True
            ok = await client.set(self.key, self.ident, nx=True, px=self.ttl_ms)
            if ok:
                self.is_leader = True
                self._last_acquire = now
                return True
            cur = await client.get(self.key)
            self.is_leader = cur == self.ident
            return self.is_leader
        except Exception:
            # Redis 抖动：保持现状（宁可不抢锁，也不让任务停摆）
            return self.is_leader

    async def release(self) -> None:
        client = await self._client()
        if client is None:
            self.is_leader = False
            return
        try:
            if (await client.get(self.key)) == self.ident:
                await client.delete(self.key)
        except Exception:
            pass
        self.is_leader = False

    async def status(self) -> dict:
        client = await self._client()
        if client is None:
            return {"backend": "single-instance", "leader": True,
                    "holder": self.ident, "note": "未配置 Redis → 单机模式恒为 leader"}
        try:
            holder = await client.get(self.key)
        except Exception:
            holder = None
        return {"backend": "redis", "leader": self.is_leader,
                "holder": holder or "", "me": self.ident}


_leader: Leader | None = None


def get_leader() -> Leader:
    global _leader
    if _leader is None:
        _leader = Leader()
    return _leader


def leader_only(fn):
    """装饰调度 handler：仅 leader 执行（单机模式无影响）

    用法：
        @leader_only
        async def _job(payload=None): ...
    非 leader 时返回 {"skipped": "not-leader"}，便于日志与测试断言。
    """
    import functools

    @functools.wraps(fn)
    async def wrapper(payload=None):
        leader = get_leader()
        if not await leader.acquire():
            return {"skipped": "not-leader", "instance": leader.ident}
        return await fn(payload)

    return wrapper
