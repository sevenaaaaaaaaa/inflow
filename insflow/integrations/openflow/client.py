"""Insight Flow → OpenFlow 集成客户端

零插件快速通道（集成方案 §3.2）：
IF 洞察直接推 OpenFlow `POST /api/webhook.php`（InboundReceiver，
`X-Inbound-Signature` = HMAC-SHA256(rawBody, secret)，类型 lead/cdp_event/contact）
→ 情报立即落 CDP 画像/线索 → 被 GrowthBrain、分群、自动化画布全链路消费。

无需改 OpenFlow 代码（在 OpenFlow 后台建一条 inbound 连接器即可）。
"""

import hashlib
import hmac
import json
import os
from datetime import UTC, datetime

import httpx


class OpenFlowError(Exception):
    pass


def sign_body(raw_body: bytes, secret: str) -> str:
    """HMAC-SHA256(rawBody, secret) —— 与 OpenFlow InboundReceiver::inbound_verify 对齐"""
    return hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()


class OpenFlowClient:
    """OpenFlow HTTP 客户端（零插件通道）"""

    def __init__(self, base_url: str | None = None, secret: str | None = None):
        self.base_url = (base_url or os.environ.get("OPENFLOW_BASE_URL", "")).rstrip("/")
        self.secret = secret or os.environ.get("OPENFLOW_WEBHOOK_SECRET", "")

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.secret)

    async def push_inbound(
        self,
        connector_id: str,
        payload: dict,
        inbound_type: str = "cdp_event",
    ) -> dict:
        """推送数据到 OpenFlow webhook.php（InboundReceiver）

        Args:
            connector_id: OpenFlow 后台创建的入站连接器 ID
            payload: 业务载荷（cdp_event: event/visitor_id + 自定义属性）
            inbound_type: lead | cdp_event | contact（用于日志与默认 event）
        Returns:
            OpenFlow 响应 {"ok": bool, ...}
        """
        if not self.configured:
            raise OpenFlowError("OPENFLOW_BASE_URL / OPENFLOW_WEBHOOK_SECRET 未配置")

        url = f"{self.base_url}/api/webhook.php"
        body = json.dumps({"connector": connector_id, **payload}, ensure_ascii=False).encode()
        signature = sign_body(body, self.secret)

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Inbound-Signature": signature,
                },
                timeout=30.0,
            )
            resp.raise_for_status()
            return resp.json()

    async def push_insight_to_cdp(self, insight: dict, connector_id: str) -> dict:
        """把洞察作为 cdp_event 推送（零插件通道核心）

        落 CDP 后：GrowthBrain / 分群 / 自动化画布均可读取 insight 来源属性。
        """
        event_payload = {
            "event": "insight_received",
            "visitor_id": insight.get("workspace_id", "insflow"),
            "insight_id": insight.get("id", ""),
            "insight_type": insight.get("type", ""),
            "title": insight.get("title", ""),
            "summary": insight.get("summary", ""),
            "severity": insight.get("severity", "medium"),
            "confidence": insight.get("confidence", 0.5),
            "source_system": "insight-flow",
            "captured_at": datetime.now(UTC).isoformat(),
        }
        return await self.push_inbound(connector_id, event_payload, "cdp_event")
