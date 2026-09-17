"""Insight Flow TTL 缓存（性能守则）

教训来源（OpenFlow）：页面/接口被高频调用时，若每次都全表扫描或重复计算，
会拖垮 DB 与响应时间。对策：
1. **TTL 缓存**：驾驶舱聚合结果短时缓存（默认 60s），页面刷新不重复打库
2. **单飞（single-flight）**：同一 key 并发只计算一次，防"缓存击穿"导致并发打库
3. **按 key 前缀失效**：数据变更时可精确清理相关缓存
4. 无外部依赖（进程内），私有化零运维

注意：进程内缓存，多 worker 部署时各自缓存（可接受，TTL 短）；
需要跨进程一致性时可换文件/Redis 实现（预留 backend 抽象）。
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass
class _Entry:
    value: Any
    expires_at: float
    created_at: float


class TTLCache:
    """进程内 TTL 缓存 + 单飞"""

    def __init__(self, default_ttl: float = 60.0, max_entries: int = 512):
        self.default_ttl = default_ttl
        self.max_entries = max_entries
        self._data: dict[str, _Entry] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._hits = 0
        self._misses = 0

    # ========== 基础读写 ==========

    def get(self, key: str) -> Any | None:
        entry = self._data.get(key)
        if entry is None:
            self._misses += 1
            return None
        if entry.expires_at < time.time():
            self._data.pop(key, None)
            self._misses += 1
            return None
        self._hits += 1
        return entry.value

    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        if len(self._data) >= self.max_entries:
            self._evict()
        now = time.time()
        self._data[key] = _Entry(value=value, created_at=now,
                                 expires_at=now + (self.default_ttl if ttl is None else ttl))

    def _evict(self) -> None:
        """优先清理过期项，其次最旧项"""
        now = time.time()
        expired = [k for k, e in self._data.items() if e.expires_at < now]
        for k in expired:
            self._data.pop(k, None)
        if len(self._data) >= self.max_entries:
            oldest = sorted(self._data.items(), key=lambda kv: kv[1].created_at)
            for k, _ in oldest[: max(1, self.max_entries // 4)]:
                self._data.pop(k, None)

    # ========== 单飞计算 ==========

    async def get_or_compute(self, key: str, factory: Callable[[], Awaitable[Any]],
                             ttl: float | None = None) -> Any:
        cached = self.get(key)
        if cached is not None:
            return cached
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            # 双重检查：等锁期间可能已被其他协程算好
            cached = self.get(key)
            if cached is not None:
                return cached
            value = await factory()
            self.set(key, value, ttl)
            return value

    # ========== 失效与观测 ==========

    def invalidate(self, prefix: str = "") -> int:
        """按前缀失效（prefix 为空则清空）"""
        if not prefix:
            n = len(self._data)
            self._data.clear()
            return n
        keys = [k for k in self._data if k.startswith(prefix)]
        for k in keys:
            self._data.pop(k, None)
        return len(keys)

    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "entries": len(self._data),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total, 3) if total else 0.0,
        }


# 全局缓存（驾驶舱/聚合共用）
cache = TTLCache(default_ttl=60.0)


def cached(key_builder: Callable[..., str], ttl: float = 60.0):
    """异步函数缓存装饰器（key 由参数构造，便于精确失效）"""
    def decorator(fn):
        async def wrapper(*args, **kwargs):
            key = key_builder(*args, **kwargs)
            return await cache.get_or_compute(key, lambda: fn(*args, **kwargs), ttl)
        wrapper.__name__ = getattr(fn, "__name__", "wrapped")
        return wrapper
    return decorator
