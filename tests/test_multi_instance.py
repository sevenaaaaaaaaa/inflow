"""Batch11 测试：多实例（Redis 客户端 / 缓存后端 / leader 选举 / 跨实例广播）

用**内置假 Redis 服务**（asyncio 讲 RESP 协议）验证客户端真实可用，
不依赖本机是否装了 Redis；没有假服务就测不到真行为，所以这里自己起一个。
"""

import asyncio
import json
import threading

import pytest


# ---------------- 假 Redis（RESP 服务） ----------------

def _bulk(value: str) -> bytes:
    """RESP 批量字符串：长度必须是**字节数**（中文与字符数不同，写错会让客户端解析错位）"""
    data = value.encode("utf-8")
    return f"${len(data)}\r\n".encode() + data + b"\r\n"


class FakeRedis:
    """假 Redis（独立线程 + 自己的事件循环）

    必须独立跑：pytest-asyncio 的 fixture loop 在 sync 测试期间不再推进，
    若服务与客户端在同一 loop，TestClient 的 portal 线程会永远等不到响应（实测卡死）。
    """

    def __init__(self):
        self.store: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.subscribers: dict[str, list[asyncio.Queue]] = {}
        self.expires: dict[str, float] = {}
        self.commands: list[list[str]] = []
        self.port = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._server: asyncio.AbstractServer | None = None

    # ---- 线程内运行 ----
    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())
        self._loop.run_forever()

    async def _serve(self):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        self._ready.set()

    def start(self) -> str:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=10)
        return f"redis://127.0.0.1:{self.port}/0"

    def stop(self):
        if self._loop and self._server:
            self._loop.call_soon_threadsafe(self._server.close)
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)

    # ---- 协议处理（在服务线程的 loop 里） ----
    async def _handle(self, reader, writer):
        try:
            while True:
                args = await self._read_command(reader)
                if args is None:
                    return
                self.commands.append(args)
                if args[0].upper() == "SUBSCRIBE":
                    await self._do_subscribe(args, writer)
                    return
                await self._dispatch(args, writer)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            return
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _read_command(self, reader) -> list[str] | None:
        line = await reader.readline()
        if not line:
            return None
        if not line.startswith(b"*"):
            return line.strip().split()
        n = int(line[1:-2])
        out = []
        for _ in range(n):
            head = await reader.readline()
            size = int(head[1:-2])
            data = await reader.readexactly(size + 2)
            out.append(data[:-2].decode())
        return out

    async def _do_subscribe(self, args: list[str], writer):
        channel = args[1]
        q: asyncio.Queue = asyncio.Queue()
        self.subscribers.setdefault(channel, []).append(q)
        writer.write(b"*3\r\n$9\r\nsubscribe\r\n" + _bulk(channel) + b":1\r\n")
        await writer.drain()
        while True:
            msg = await q.get()
            writer.write(b"*3\r\n$7\r\nmessage\r\n" + _bulk(channel) + _bulk(msg))
            await writer.drain()

    async def _dispatch(self, args: list[str], writer):
        cmd = args[0].upper()
        if cmd == "PING":
            writer.write(b"+PONG\r\n")
        elif cmd in ("AUTH", "SELECT"):
            writer.write(b"+OK\r\n")
        elif cmd == "GET":
            val = self.store.get(args[1])
            writer.write(b"$-1\r\n" if val is None
                         else _bulk(val))
        elif cmd == "SET":
            key, val = args[1], args[2]
            nx = "NX" in [a.upper() for a in args[3:]]
            if nx and key in self.store:
                writer.write(b"$-1\r\n")
            else:
                self.store[key] = val
                for i, a in enumerate(args):
                    if a.upper() == "PX":
                        self.expires[key] = (self._loop.time() + int(args[i + 1]) / 1000)
                writer.write(b"+OK\r\n")
        elif cmd == "DEL":
            n = sum(1 for k in args[1:] if self.store.pop(k, None) is not None)
            writer.write(f":{n}\r\n".encode())
        elif cmd == "EXPIRE":
            self.expires[args[1]] = self._loop.time() + int(args[2])
            writer.write(b":1\r\n")
        elif cmd == "INCR":
            self.store[args[1]] = str(int(self.store.get(args[1], "0")) + 1)
            writer.write(f":{self.store[args[1]]}\r\n".encode())
        elif cmd == "HSET":
            self.hashes.setdefault(args[1], {})[args[2]] = args[3]
            writer.write(b":1\r\n")
        elif cmd == "HDEL":
            h = self.hashes.get(args[1], {})
            n = sum(1 for f in args[2:] if h.pop(f, None) is not None)
            writer.write(f":{n}\r\n".encode())
        elif cmd == "HGETALL":
            h = self.hashes.get(args[1], {})
            payload = b"".join(_bulk(k) + _bulk(v) for k, v in h.items())
            writer.write(f"*{len(h) * 2}\r\n".encode() + payload)
        elif cmd == "PUBLISH":
            delivered = 0
            for q in self.subscribers.get(args[1], []):
                self._loop.call_soon_threadsafe(q.put_nowait, args[2])
                delivered += 1
            writer.write(f":{delivered}\r\n".encode())
        else:
            writer.write(f"-ERR unknown command {cmd}\r\n".encode())
        await writer.drain()


@pytest.fixture
def fake_redis(monkeypatch):
    """同步 fixture：假 Redis 在独立线程里跑，任何 loop 的客户端都能连"""
    server = FakeRedis()
    url = server.start()
    monkeypatch.setenv("INSFLOW_REDIS_URL", url)
    from insflow.core.db import redis_backend as rb
    rb.reset_redis()
    yield server
    server.stop()
    rb.reset_redis()
    monkeypatch.delenv("INSFLOW_REDIS_URL", raising=False)


class TestRedisClient:
    async def test_basic_commands(self, fake_redis):
        from insflow.core.db.redis_backend import RedisClient
        c = RedisClient()
        await c.connect()
        assert await c.ping() is True
        assert await c.set("k", "v") is True
        assert await c.get("k") == "v"
        assert await c.incr("n") == 1 and await c.incr("n") == 2
        assert await c.delete("k") == 1 and await c.get("k") is None
        await c.close()

    async def test_set_nx_semantics(self, fake_redis):
        from insflow.core.db.redis_backend import RedisClient
        c = RedisClient()
        await c.connect()
        assert await c.set("lock", "a", nx=True, px=5000) is True
        assert await c.set("lock", "b", nx=True, px=5000) is False   # 已被占
        assert await c.get("lock") == "a"
        await c.close()

    async def test_hash_and_pubsub(self, fake_redis):
        from insflow.core.db.redis_backend import RedisClient
        publisher = RedisClient()
        await publisher.connect()
        subscriber = RedisClient()
        await subscriber.connect()

        got: list[str] = []

        async def _consume():
            async for msg in subscriber.subscribe("chan"):
                got.append(msg)
                return
        task = asyncio.create_task(_consume())
        await asyncio.sleep(0.1)
        await publisher.publish("chan", json.dumps({"hello": "world"}))
        await asyncio.wait_for(task, timeout=3)
        assert json.loads(got[0])["hello"] == "world"

        assert await publisher.hset("h", "f1", "v1") == 1
        assert (await publisher.hgetall("h"))["f1"] == "v1"
        assert await publisher.hdel("h", "f1") == 1
        await publisher.close()
        await subscriber.close()

    def test_parse_url(self):
        from insflow.core.db.redis_backend import parse_url
        cfg = parse_url("redis://:secret@redis.internal:6380/2")
        assert cfg == {"host": "redis.internal", "port": 6380, "password": "secret",
                       "db": 2, "tls": False}
        assert parse_url("redis://127.0.0.1").get("port") == 6379


class TestLeaderElection:
    async def test_single_leader_among_instances(self, fake_redis):
        from insflow.core.leader import Leader
        a, b, c = Leader(ident="a"), Leader(ident="b"), Leader(ident="c")
        assert await a.acquire() is True          # 第一个抢到
        assert await b.acquire() is False         # 其它实例拿不到
        assert await c.acquire() is False
        status = await a.status()
        assert status["backend"] == "redis" and status["leader"] is True
        assert status["holder"] == "a"

    async def test_renew_and_release_then_takeover(self, fake_redis):
        from insflow.core.leader import Leader
        a, b = Leader(ident="a"), Leader(ident="b")
        assert await a.acquire() is True
        assert await a.acquire() is True          # 续租仍是自己
        await a.release()
        assert await b.acquire() is True          # 释放后 b 可接管
        assert await a.acquire() is False

    async def test_single_instance_fallback(self, monkeypatch):
        monkeypatch.delenv("INSFLOW_REDIS_URL", raising=False)
        from insflow.core.db import redis_backend as rb
        rb.reset_redis()
        from insflow.core.leader import Leader, leader_only
        leader = Leader(ident="solo")
        assert await leader.acquire() is True
        assert (await leader.status())["backend"] == "single-instance"

        @leader_only
        async def _job(payload=None):
            return {"ran": True}
        assert (await _job()) == {"ran": True}

    async def test_job_skipped_on_non_leader(self, fake_redis):
        from insflow.core.leader import Leader, get_leader, leader_only
        holder = Leader(ident="holder")
        assert await holder.acquire() is True
        # 让全局 leader 成为"另一个实例"
        import insflow.core.leader as mod
        mod._leader = Leader(ident="other")

        @leader_only
        async def _job(payload=None):
            return {"ran": True}
        out = await _job()
        assert out.get("skipped") == "not-leader"
        mod._leader = None
        get_leader()


class TestRedisCacheAndRealtime:
    async def test_cache_shared_across_instances(self, fake_redis):
        """实例 A 算出的值，实例 B 直接命中（不再重复计算）"""
        from insflow.core.cache import TTLCache
        from insflow.core.db.redis_backend import RedisCacheBackend

        backend_a = RedisCacheBackend()
        cache_a = TTLCache(backend=backend_a)
        calls = {"n": 0}

        async def factory():
            calls["n"] += 1
            return {"v": 42}
        first = await cache_a.get_or_compute("k1", factory, ttl=60)
        assert first == {"v": 42} and calls["n"] == 1

        backend_b = RedisCacheBackend()
        cache_b = TTLCache(backend=backend_b)

        async def factory_b():
            calls["n"] += 1
            return {"v": -1}
        second = await cache_b.get_or_compute("k1", factory_b, ttl=60)
        assert second == {"v": 42} and calls["n"] == 1      # 未重复计算

    async def test_realtime_broadcast_and_presence(self, fake_redis):
        from insflow.engine.realtime import (RedisPresence, broadcast,
                                            subscribe_broadcast)

        events = []

        async def _listen():
            async for ev, data in subscribe_broadcast():
                events.append((ev, data))
                return
        task = asyncio.create_task(_listen())
        await asyncio.sleep(0.1)
        n = await broadcast("metrics", {"workspace_id": "w1", "value": 1})
        await asyncio.wait_for(task, timeout=3)
        assert n >= 1 and events[0][0] == "metrics"

        pres = RedisPresence()
        await pres.touch_async("w1", "c1", user="seven", cursor={"x": 0.5, "y": 0.5})
        await pres.touch_async("w2", "c2", user="alice")
        snap = await pres.snapshot_async("w1")
        assert [e["conn_id"] for e in snap] == ["c1"]
        assert snap[0]["cursor"] == {"x": 0.5, "y": 0.5}
        await pres.leave_async("w1", "c1")
        assert await pres.snapshot_async("w1") == []

    async def test_presence_expires(self, fake_redis, monkeypatch):
        from insflow.engine import realtime as rt
        monkeypatch.setattr(rt, "PRESENCE_TTL", 0.001)
        pres = rt.RedisPresence()
        await pres.touch_async("w1", "c1", user="x")
        await asyncio.sleep(0.01)
        assert await pres.snapshot_async("w1") == []

    async def test_observability_reports_backend(self, fake_redis):
        from fastapi.testclient import TestClient
        from insflow.server.app import app
        body = TestClient(app).get("/api/v1/observability").json()
        assert body["cache_backend"] in ("redis", "memory")

    async def test_presence_api_uses_shared_backend(self, fake_redis):
        from fastapi.testclient import TestClient
        from insflow.server.app import app
        cli = TestClient(app)
        r = cli.post("/api/v1/presence", json={
            "workspace_id": "w1", "conn_id": "c1",
            "cursor": {"x": 0.1, "y": 0.2}}).json()
        assert r["backend"] == "RedisPresence"
        listed = cli.get("/api/v1/presence", params={"workspace_id": "w1"}).json()
        assert listed["backend"] == "RedisPresence"
        assert [e["conn_id"] for e in listed["online"]] == ["c1"]


class TestDeployScale:
    def test_scale_compose_and_env_docs(self):
        from pathlib import Path
        compose = Path("deploy/docker/docker-compose.scale.yml")
        assert compose.exists()
        text = compose.read_text(encoding="utf-8")
        assert "redis:" in text and "INSFLOW_REDIS_URL" in text
        assert text.count("insflow") >= 2          # 至少两个应用实例
        env_example = Path(".env.example").read_text(encoding="utf-8")
        assert "INSFLOW_REDIS_URL" in env_example
