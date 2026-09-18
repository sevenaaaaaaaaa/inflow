"""跨系统契约测试用「假对端」（独立线程 + 自己的事件循环）

为什么要独立线程：pytest-asyncio 的 loop 在 sync 测试期间不推进，
服务与客户端同 loop 会死等（与 FakeRedis 同样的坑）。真实 HTTP + HMAC 校验，
才能验证签名/幂等/失败路径这些**契约**，而不是只测 mock。
"""
import asyncio
import contextlib
import hashlib
import hmac
import json
import threading
from typing import Any


class FakeHTTPPeer:
    """极简 HTTP/1.1 假服务：可配置路由响应 + 记录请求 + 可选 HMAC 校验"""

    def __init__(self, secret: str = "", routes: dict | None = None,
                 signature_header: str = ""):
        self.secret = secret
        # 兼容两种契约：零插件通道 X-Inbound-Signature / 插件通道 X-Insight-Flow-Signature
        self.signature_headers = [signature_header] if signature_header else [
            "x-inbound-signature", "x-insight-flow-signature"]
        self.routes = routes or {}
        self.requests: list[dict] = []
        self.port = 0
        self._server: asyncio.AbstractServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    # ---- 生命周期 ----
    def start(self) -> str:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=10)
        return f"http://127.0.0.1:{self.port}"

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())
        self._loop.run_forever()

    async def _serve(self):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        self._ready.set()

    def stop(self):
        if self._loop and self._server:
            self._loop.call_soon_threadsafe(self._server.close)
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)

    # ---- 请求处理 ----
    async def _handle(self, reader, writer):
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            parts = request_line.decode().split()
            method, path = (parts + ["", ""])[:2]
            headers: dict[str, str] = {}
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                k, _, v = line.decode().partition(":")
                headers[k.strip().lower()] = v.strip()
            body = b""
            length = int(headers.get("content-length") or 0)
            if length:
                body = await reader.readexactly(length)
            record = {"method": method, "path": path, "headers": headers,
                      "body": body.decode("utf-8", "ignore")}
            record["json"] = {}
            with contextlib.suppress(json.JSONDecodeError):
                record["json"] = json.loads(record["body"] or "{}")
            record["signature_ok"] = self._check_signature(headers, body)
            self.requests.append(record)

            status, payload = self._route(method, path, record)
            raw = json.dumps(payload, ensure_ascii=False).encode()
            writer.write(
                f"HTTP/1.1 {status} OK\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(raw)}\r\nConnection: close\r\n\r\n".encode() + raw)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    def _check_signature(self, headers: dict, body: bytes) -> bool | None:
        if not self.secret:
            return None
        provided = next((headers[h] for h in self.signature_headers
                         if headers.get(h)), "")
        if not provided:
            return False
        expected = hmac.new(self.secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, provided)

    def _route(self, method: str, path: str, record: dict) -> tuple[int, Any]:
        for pattern, handler in self.routes.items():
            if path.startswith(pattern):
                result = handler(record) if callable(handler) else handler
                if isinstance(result, tuple):
                    return result
                return 200, result
        return 200, {"ok": True}

    # ---- 断言辅助 ----
    def find(self, path_part: str) -> list[dict]:
        return [r for r in self.requests if path_part in r["path"]]

    def bodies(self, path_part: str) -> list[dict]:
        return [r["json"] for r in self.find(path_part)]
