"""Insight Flow → MFlow 集成客户端

调用 MFlow 控制台 HTTP API（1-4 Dev/console/console.py）：
    认证：POST /api/login（密码换 session cookie；密码存 IF 保险库）
    派发：POST /api/loop/create {topic, brief, template_id?, max_rounds:3} → {ok, id, queued}
    轮询：GET /api/loop/detail?id=... → status: queued|running|done
    取稿：GET /api/read?path=...（白名单扩展名+项目内路径）→ 草稿 Markdown
    状态：POST /api/item/upsert + /api/item/advance（登记进 MFlow 12 阶段状态机）

设计约束（集成方案 §2.2）：
- 发布永远停在人工授权后（MFlow 铁律）——IF 只创建 Loop 与草稿，绝不替用户点"发布"
- 批量场景 loop/create + 轮询
"""

import os
from typing import Optional

import httpx


class MFlowError(Exception):
    pass


class MFlowClient:
    """MFlow 控制台客户端（session cookie 认证）"""

    def __init__(self, base_url: str | None = None, password: str | None = None):
        self.base_url = (base_url or os.environ.get("MFLOW_BASE_URL", "")).rstrip("/")
        self.password = password or os.environ.get("MFLOW_CONSOLE_PASSWORD", "")
        self._cookie: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.password)

    async def _ensure_session(self) -> None:
        """登录换 session cookie"""
        if self._cookie:
            return
        if not self.configured:
            raise MFlowError("MFLOW_BASE_URL / MFLOW_CONSOLE_PASSWORD 未配置")
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/api/login",
                json={"password": self.password},
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok", False):
                raise MFlowError(f"MFlow 登录失败: {data}")
            set_cookie = resp.headers.get("set-cookie", "")
            if set_cookie:
                self._cookie = set_cookie.split(";")[0]

    def _auth_headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self._cookie:
            headers["Cookie"] = self._cookie
        return headers

    async def create_loop(self, topic: str, brief: str, template_id: str | None = None,
                          max_rounds: int = 3, item_id: str | None = None) -> dict:
        """派发产稿 Loop（异步）→ {ok, id, queued}"""
        await self._ensure_session()
        payload = {"topic": topic, "brief": brief, "max_rounds": max_rounds}
        if item_id:
            payload["item_id"] = item_id
        if template_id:
            payload["template_id"] = template_id

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/api/loop/create",
                json=payload,
                headers=self._auth_headers(),
                timeout=30.0,
            )
            resp.raise_for_status()
            return resp.json()

    async def loop_detail(self, loop_id: str) -> dict:
        """轮询 Loop 状态 → status: queued|running|done"""
        await self._ensure_session()
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self.base_url}/api/loop/detail",
                params={"id": loop_id},
                headers=self._auth_headers(),
                timeout=15.0,
            )
            resp.raise_for_status()
            return resp.json()

    async def poll_until_done(self, loop_id: str, max_polls: int = 20,
                              interval_seconds: float = 3.0) -> dict:
        """轮询直到 done / running 超时

        MFlow 是异步排程（P5），通常几十秒内完成；
        超时则返回最后状态，由调用方决定后续（再次轮询或放弃）。
        """
        import asyncio
        for i in range(max_polls):
            data = await self.loop_detail(loop_id)
            status = data.get("status", "")
            if status == "done":
                return data
            if status not in ("queued", "running", ""):
                raise MFlowError(f"MFlow Loop 异常状态: {status}")
            if i < max_polls - 1:
                await asyncio.sleep(interval_seconds)
        return {"ok": False, "status": "timeout", "loop_id": loop_id}

    async def read_file(self, path: str) -> str:
        """取稿（GET /api/read?path=...，白名单扩展名）"""
        await self._ensure_session()
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self.base_url}/api/read",
                params={"path": path},
                headers=self._auth_headers(),
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok", False):
                raise MFlowError(f"MFlow 取稿失败: {data}")
            return data.get("content", "")

    async def upsert_item(self, item: dict) -> dict:
        """登记选题进 MFlow 12 阶段状态机"""
        await self._ensure_session()
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/api/item/upsert",
                json=item,
                headers=self._auth_headers(),
                timeout=15.0,
            )
            resp.raise_for_status()
            return resp.json()

    async def advance_item(self, item_id: str, to_stage: str | None = None) -> dict:
        """推进选题状态"""
        await self._ensure_session()
        payload = {"item_id": item_id}
        if to_stage:
            payload["to"] = to_stage
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/api/item/advance",
                json=payload,
                headers=self._auth_headers(),
                timeout=15.0,
            )
            resp.raise_for_status()
            return resp.json()

    async def check_health(self) -> bool:
        """健康检查（不触发完整登录）"""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{self.base_url}/health", timeout=5.0)
                return resp.status_code == 200
        except Exception:
            return False
