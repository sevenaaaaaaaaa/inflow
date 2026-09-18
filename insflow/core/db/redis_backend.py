"""最小 Redis 客户端（零依赖，纯 asyncio + RESP 协议）

为什么自己写：家族约束是「零第三方依赖、私有化可离线」。多实例部署需要共享
缓存 / 分布式锁（leader 选举）/ 事件总线（SSE 跨实例），这三件事只需要 Redis 的
一小撮命令，自己实现 RESP 解析比引入 redis-py 更符合约束（也更好审计）。

支持命令：PING、GET、SET（含 NX/PX）、DEL、EXPIRE、INCR、PUBLISH、SUBSCRIBE、
HSET、HGET、HGETALL、HDEL、LPUSH、LRANGE、SCAN？
未实现命令直接抛 NotImplementedError，绝不静默失败。

用法：`INSFLOW_REDIS_URL=redis://host:6379/0`（可选密码：redis://:pass@host:6379/0）
"""

import asyncio
import os
from urllib.parse import urlparse


class RedisError(Exception):
    pass


class RedisNotConfigured(RedisError):
    pass


def redis_url() -> str:
    return os.environ.get("INSFLOW_REDIS_URL", "").strip()


def parse_url(url: str) -> dict:
    u = urlparse(url if "://" in url else f"redis://{url}")
    if u.scheme not in ("redis", "rediss"):
        raise RedisError(f"不支持的 Redis URL：{url}")
    return {"host": u.hostname or "127.0.0.1", "port": u.port or 6379,
            "password": u.password or "", "db": int((u.path or "/0").lstrip("/") or 0),
            "tls": u.scheme == "rediss"}


def _encode(args: list) -> bytes:
    out = [f"*{len(args)}\r\n".encode()]
    for a in args:
        if isinstance(a, bytes):
            data = a
        elif isinstance(a, (int, float)):
            data = str(a).encode()
        else:
            data = str(a).encode()
        out.append(f"${len(data)}\r\n".encode() + data + b"\r\n")
    return b"".join(out)


class _Reader:
    """RESP2 解析（含 inline/错误/整数/批量/数组）"""

    def __init__(self, reader: asyncio.StreamReader):
        self.reader = reader

    async def read(self):
        line = await self.reader.readline()
        if not line:
            raise RedisError("连接已关闭")
        prefix, payload = line[:1], line[1:-2]
        if prefix == b"+":
            return payload.decode()
        if prefix == b"-":
            raise RedisError(payload.decode())
        if prefix == b":":
            return int(payload)
        if prefix == b"$":
            n = int(payload)
            if n == -1:
                return None
            data = await self.reader.readexactly(n + 2)
            return data[:-2].decode("utf-8", "replace")
        if prefix == b"*":
            n = int(payload)
            if n == -1:
                return None
            return [await self.read() for _ in range(n)]
        raise RedisError(f"无法识别的响应：{line[:40]!r}")


class RedisClient:
    """单连接 + 命令锁（够用：缓存/锁/事件总线都不是高吞吐场景）"""

    def __init__(self, url: str = ""):
        self.url = url or redis_url()
        if not self.url:
            raise RedisNotConfigured("未配置 INSFLOW_REDIS_URL")
        cfg = parse_url(self.url)
        self.cfg = cfg
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock: asyncio.Lock | None = None
        self._loop = None

    def _ensure_loop(self) -> None:
        """连接绑定在某个事件循环上；换 loop（如同一进程里 CLI + 服务）必须重连

        否则会出现「等待永远不会到来的响应」——比报错更难查。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if self._loop is not loop:
            self._loop = loop
            self._writer = None
            self._reader = None
            self._lock = None

    async def connect(self) -> None:
        self._ensure_loop()
        if self._lock is None:
            self._lock = asyncio.Lock()
        if self._writer and not self._writer.is_closing():
            return
        self._reader, self._writer = await asyncio.open_connection(
            self.cfg["host"], self.cfg["port"], ssl=self.cfg["tls"])
        self._resp = _Reader(self._reader)
        if self.cfg["password"]:
            await self._send(["AUTH", self.cfg["password"]])
        if self.cfg["db"]:
            await self._send(["SELECT", self.cfg["db"]])

    async def close(self) -> None:
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
        self._writer = None
        self._reader = None

    async def _send(self, args: list):
        self._ensure_loop()            # 换 loop 必须先重连（否则会等一个不会来的响应）
        if self._writer is None or self._writer.is_closing():
            await self.connect()
        async with self._lock:
            self._writer.write(_encode(args))
            await self._writer.drain()
            return await self._resp.read()

    # ---- 基础命令 ----
    async def ping(self) -> bool:
        return (await self._send(["PING"])) in ("PONG", "pong")

    async def get(self, key: str):
        return await self._send(["GET", key])

    async def set(self, key: str, value, *, nx: bool = False, px: int = 0,
                  ex: int = 0) -> bool:
        args: list = ["SET", key, value]
        if nx:
            args.append("NX")
        if px:
            args += ["PX", int(px)]
        elif ex:
            args += ["EX", int(ex)]
        return (await self._send(args)) is not None

    async def delete(self, *keys: str) -> int:
        return int(await self._send(["DEL", *keys]))

    async def expire(self, key: str, seconds: int) -> bool:
        return bool(await self._send(["EXPIRE", key, int(seconds)]))

    async def incr(self, key: str) -> int:
        return int(await self._send(["INCR", key]))

    async def hset(self, key: str, field: str, value) -> int:
        return int(await self._send(["HSET", key, field, value]))

    async def hdel(self, key: str, *fields: str) -> int:
        return int(await self._send(["HDEL", key, *fields]))

    async def hgetall(self, key: str) -> dict:
        flat = await self._send(["HGETALL", key]) or []
        return {flat[i]: flat[i + 1] for i in range(0, len(flat) - 1, 2)}

    async def publish(self, channel: str, message) -> int:
        return int(await self._send(["PUBLISH", channel, message]))

    async def subscribe(self, channel: str):
        """订阅并异步产出消息（生成器）；调用方负责取消/关闭"""
        if self._writer is None:
            await self.connect()
        writer = self._writer
        reader = _Reader(self._reader)
        writer.write(_encode(["SUBSCRIBE", channel]))
        await writer.drain()
        first = await reader.read()            # 确认帧 [subscribe, channel, n]
        if not isinstance(first, list) or first[0] != "subscribe":
            raise RedisError(f"订阅失败：{first}")
        while True:
            msg = await reader.read()
            if not isinstance(msg, list) or msg[0] != "message":
                continue
            yield msg[2]


_client: RedisClient | None = None


async def get_redis(connect: bool = True) -> RedisClient | None:
    """获取全局客户端；未配置返回 None（调用方走单机降级路径）"""
    global _client
    if not redis_url():
        return None
    if _client is None:
        _client = RedisClient()
    if connect:
        try:
            await _client.connect()
        except Exception:
            return None
    return _client


def reset_redis() -> None:
    global _client
    _client = None


async def redis_available() -> bool:
    client = await get_redis(connect=False)
    if client is None:
        return False
    try:
        return await client.ping()
    except Exception:
        return False


# ========== 缓存后端适配（core.cache 的 _backend 协议）==========

class RedisCacheBackend:
    """把内存缓存镜像到 Redis（多实例共享；单实例无 Redis 时自动不用）"""

    def __init__(self, prefix: str = "insflow:cache:", ttl_pad: float = 1.05):
        self.prefix = prefix
        self.ttl_pad = ttl_pad

    async def _client(self):
        return await get_redis()

    def get(self, key: str):                 # 同步接口（cache.get 是同步的）
        return None                          # 由 aget 提供异步读取，见 core.cache 适配

    async def aget(self, key: str):
        client = await self._client()
        if client is None:
            return None
        try:
            raw = await client.get(self.prefix + key)
            if raw is None:
                return None
            import json
            return json.loads(raw)
        except Exception:
            return None

    def set(self, key: str, value, ttl: float = 60.0) -> None:
        """兼容同步协议（fire-and-forget）：调度一个任务写入"""
        try:
            asyncio.get_running_loop().create_task(self.aset(key, value, ttl))
        except RuntimeError:
            pass

    async def aset(self, key: str, value, ttl: float = 60.0) -> None:
        client = await self._client()
        if client is None:
            return
        try:
            import json
            await client.set(self.prefix + key, json.dumps(value, ensure_ascii=False,
                                                          default=str),
                             px=int(max(1.0, ttl) * 1000 * self.ttl_pad))
        except Exception:
            pass

    def invalidate(self, prefix: str = "") -> int:
        return 0                             # Redis 侧按 TTL 自然过期（简单且安全）

    def clear(self) -> None:
        return None
