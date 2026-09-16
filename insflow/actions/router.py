"""Insight Flow Action Router（动作路由器）

动作路由器：Insight.recommended_actions → 目标系统调用（PRD AC-3）
统一接口：execute(action, ctx) -> {ok, ref, detail}，结果写 actions 表并可被验证状态机追踪。

适配器映射表（集成方案 §4）：
| openflow.webhook_insight | OpenFlow | POST /api/webhook.php（InboundReceiver HMAC）|
| webhook.generic          | n8n/Zapier/Make/Dify | 出站 Webhook（HMAC）|
"""

import hashlib
import hmac
import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx

from ..integrations.openflow import OpenFlowClient


@dataclass
class ActionContext:
    """动作执行上下文"""
    workspace_id: str
    insight_id: str
    metadata: dict = field(default_factory=dict)


class ActionResult(dict):
    """执行结果 {ok, ref, detail}"""

    @classmethod
    def ok(cls, ref: str = "", detail: str = "") -> "ActionResult":
        return cls({"ok": True, "ref": ref, "detail": detail})

    @classmethod
    def fail(cls, detail: str = "") -> "ActionResult":
        return cls({"ok": False, "ref": "", "detail": detail})


class ActionAdapter(ABC):
    """动作适配器基类"""

    @property
    @abstractmethod
    def action_type(self) -> str:
        ...

    @abstractmethod
    async def execute(self, action: dict, ctx: ActionContext) -> dict:
        """执行动作

        action: {action_type, target_ref, params_json, description}
        Returns: {ok: bool, ref: str, detail: str}
        """
        ...


# ========== openflow.webhook_insight ==========

class OpenFlowWebhookAdapter(ActionAdapter):
    """IF 洞察 → OpenFlow webhook.php 落 CDP（零插件通道，M1 DoD 核心）"""

    def __init__(self, client: OpenFlowClient | None = None):
        self.client = client or OpenFlowClient()

    @property
    def action_type(self) -> str:
        return "openflow.webhook_insight"

    async def execute(self, action: dict, ctx: ActionContext) -> dict:
        if not self.client.configured:
            return ActionResult.fail("OPENFLOW_BASE_URL / OPENFLOW_WEBHOOK_SECRET 未配置")

        connector_id = action.get("target_ref") or action.get("params_json", {}).get(
            "connector_id", os.environ.get("OPENFLOW_INBOUND_CONNECTOR_ID", "")
        )
        if not connector_id:
            return ActionResult.fail("缺少 OpenFlow inbound 连接器 ID（target_ref）")

        # 组装洞察载荷（params 可覆盖/扩展）
        insight_payload = {
            "id": ctx.insight_id,
            "workspace_id": ctx.workspace_id,
            "type": action.get("insight_type", ""),
            "title": action.get("title", ""),
            "summary": action.get("summary", ""),
            "severity": action.get("severity", "medium"),
            "confidence": action.get("confidence", 0.5),
            **action.get("params_json", {}),
        }

        try:
            result = await self.client.push_insight_to_cdp(insight_payload, connector_id)
            ok = bool(result.get("ok", False))
            ref = f"openflow:cdp:{ctx.insight_id}" if ok else ""
            return ActionResult(ref, json.dumps(result, ensure_ascii=False)) if ok \
                else ActionResult.fail(str(result.get("error", "openflow rejected")))
        except Exception as e:
            return ActionResult.fail(f"{type(e).__name__}: {e}")


# ========== webhook.generic ==========

class GenericWebhookAdapter(ActionAdapter):
    """通用出站 Webhook（HMAC-SHA256 + X-IF-Signature + 时间戳防重放）

    目标：n8n / Zapier / Make / Dify / 任意 Webhook 接收方
    """

    @property
    def action_type(self) -> str:
        return "webhook.generic"

    async def execute(self, action: dict, ctx: ActionContext) -> dict:
        params = action.get("params_json", {})
        url = action.get("target_ref") or params.get("url", "")
        secret = params.get("secret", os.environ.get("INSFLOW_WEBHOOK_SECRET", ""))
        if not url:
            return ActionResult.fail("webhook.generic 缺少目标 URL")

        payload = {
            "event": "insight.action",
            "workspace_id": ctx.workspace_id,
            "insight_id": ctx.insight_id,
            "action": action.get("action_type", self.action_type),
            "description": action.get("description", ""),
            "data": params.get("data", {}),
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        raw = json.dumps(payload, ensure_ascii=False).encode()
        headers = {"Content-Type": "application/json"}
        if secret:
            headers["X-IF-Signature"] = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, content=raw, headers=headers, timeout=30.0)
                ok = 200 <= resp.status_code < 300
                return ActionResult.ok(
                    ref=f"webhook:{resp.status_code}",
                    detail=f"status={resp.status_code}",
                ) if ok else ActionResult.fail(f"HTTP {resp.status_code}")
        except Exception as e:
            return ActionResult.fail(f"{type(e).__name__}: {e}")


# ========== Action Router ==========

class ActionRouter:
    """动作路由器"""

    def __init__(self):
        self._adapters: dict[str, ActionAdapter] = {}

    def register(self, adapter: ActionAdapter) -> None:
        self._adapters[adapter.action_type] = adapter

    def get(self, action_type: str) -> Optional[ActionAdapter]:
        return self._adapters.get(action_type)

    def list_types(self) -> list[str]:
        return sorted(self._adapters.keys())

    async def dispatch(self, action: dict, ctx: ActionContext) -> dict:
        """派发动作：查找适配器 → 执行 → 记录事件流"""
        action_type = action.get("action_type", "")
        adapter = self._adapters.get(action_type)
        if not adapter:
            return ActionResult.fail(f"未注册的动作类型: {action_type}")

        from ..core.files import EventBus
        bus = EventBus(ctx.workspace_id)
        bus.emit("action.dispatched", {
            "insight_id": ctx.insight_id,
            "action_type": action_type,
        })

        try:
            result = await adapter.execute(action, ctx)
        except Exception as e:
            result = ActionResult.fail(f"{type(e).__name__}: {e}")

        event = "action.executed" if result.get("ok") else "action.failed"
        bus.emit(event, {
            "insight_id": ctx.insight_id,
            "action_type": action_type,
            "ref": result.get("ref", ""),
            "detail": result.get("detail", ""),
        })
        return result


# 全局实例
_router: Optional[ActionRouter] = None


def get_action_router() -> ActionRouter:
    """获取全局动作路由（含内置适配器）"""
    global _router
    if _router is None:
        _router = ActionRouter()
        _router.register(OpenFlowWebhookAdapter())
        _router.register(GenericWebhookAdapter())
    return _router
