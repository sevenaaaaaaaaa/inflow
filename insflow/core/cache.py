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

    def __init__(self, default_ttl: float = 60.0, max_entries: int = 512,
                 backend=None):
        self.default_ttl = default_ttl
        self.max_entries = max_entries
        self._data: dict[str, _Entry] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._hits = 0
        self._misses = 0
        self._backend = backend  # 可选文件后端（跨进程）

    # ========== 基础读写 ==========

    def get(self, key: str) -> Any | None:
        entry = self._data.get(key)
        if entry is None and self._backend is not None:
            value = self._backend.get(key)
            if value is not None:
                self._hits += 1
                return value
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
        effective_ttl = self.default_ttl if ttl is None else ttl
        if self._backend is not None:
            try:
                self._backend.set(key, value, effective_ttl)
            except Exception:
                pass
        if len(self._data) >= self.max_entries:
            self._evict()
        now = time.time()
        self._data[key] = _Entry(value=value, created_at=now,
                                 expires_at=now + effective_ttl)

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

    async def _backend_get(self, key: str) -> Any:
        """外部后端读取（Redis 需要异步；文件后端是同步的）"""
        backend = self._backend
        if backend is None:
            return None
        aget = getattr(backend, "aget", None)
        if aget is not None:
            try:
                return await aget(key)
            except Exception:
                return None
        try:
            return backend.get(key)
        except Exception:
            return None

    async def get_or_compute(self, key: str, factory: Callable[[], Awaitable[Any]],
                             ttl: float | None = None) -> Any:
        cached = self.get(key)
        if cached is not None:
            return cached
        # 多实例：先问共享后端（Redis），命中即回填本地，避免重复计算
        external = await self._backend_get(key)
        if external is not None:
            self._data[key] = _Entry(external, time.time(),
                                     time.time() + (ttl or self.default_ttl))
            self._hits += 1
            return external
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            # 双重检查：等锁期间可能已被其他协程算好
            cached = self.get(key)
            if cached is not None:
                return cached
            external = await self._backend_get(key)
            if external is not None:
                self._data[key] = _Entry(external, time.time(),
                                     time.time() + (ttl or self.default_ttl))
                self._hits += 1
                return external
            value = await factory()
            self.set(key, value, ttl)
            if self._backend is not None:
                aset = getattr(self._backend, "aset", None)
                if aset is not None:
                    try:
                        await aset(key, value, ttl or self.default_ttl)
                    except Exception:
                        pass
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

class FileBackend:
    """文件缓存后端（对齐 OpenFlow Cache 的 FileCache：零依赖、跨进程共享）

    用途：多 worker 部署或重启后仍能命中；单进程无必要可不开（INSFLOW_CACHE=file）。
    注意：只适合中小体量键值（驾驶舱聚合结果），不做大对象。
    """

    def __init__(self, path):
        from pathlib import Path
        self.dir = Path(path)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _f(self, key: str):
        import hashlib
        return self.dir / (hashlib.sha256(key.encode()).hexdigest()[:32] + ".json")

    def get(self, key: str):
        import json
        import time
        f = self._f(key)
        if not f.exists():
            return None
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if data.get("expires_at", 0) < time.time():
            f.unlink(missing_ok=True)
            return None
        return data.get("value")

    def set(self, key: str, value, ttl: float) -> None:
        import json
        import os
        import time
        f = self._f(key)
        payload = json.dumps({"value": value, "expires_at": time.time() + ttl},
                             ensure_ascii=False, default=str)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, f)

    def clear(self) -> None:
        for f in self.dir.glob("*.json"):
            f.unlink(missing_ok=True)


def get_cache():
    """获取缓存单例

    后端优先级：INSFLOW_REDIS_URL（多实例共享）> INSFLOW_CACHE=file（跨进程文件）
    > 内存。多实例部署必须配 Redis，否则各实例缓存不一致（但不会错，只是命中率低）。
    """
    import os
    global cache
    if cache._backend is not None:
        return cache
    if os.environ.get("INSFLOW_REDIS_URL", "").strip():
        try:
            from .db.redis_backend import RedisCacheBackend
            cache._backend = RedisCacheBackend()
            return cache
        except Exception:
            pass
    if os.environ.get("INSFLOW_CACHE", "").lower() == "file":
        from .files import DATA_DIR
        cache._backend = FileBackend((DATA_DIR or ".").__str__() + "/cache")
    return cache
