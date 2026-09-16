"""Insight Flow 入站事件接收（/api/v1/ingest）

闭环的"最后一公里"（集成方案 §2.3）：
- MFlow 发布 webhook 回流：content.published → 关联 insight → 动作完成 → 验证状态机
- OpenFlow 钩子出站：cdp_event / lead / contact 事件落 IF

安全：HMAC-SHA256 验签（X-IF-Signature 或 X-Inbound-Signature，
与 OpenFlow InboundReceiver::inbound_verify 同算法），验签失败一律拒绝。
"""

import hashlib
import hmac
import json
import os

from ..core.files import EventBus
from ..core.store import get_store
from .feedback_tracker import FeedbackTracker

# 支持的签名头（与 OpenFlow 对齐 + 自有出站格式）
SIGNATURE_HEADERS = ("x-if-signature", "x-inbound-signature")


def verify_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """HMAC-SHA256(rawBody, secret) 常量时间比较"""
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def extract_signature(headers) -> str:
    """从请求头提取签名（大小写不敏感，支持两种头名）"""
    for name in SIGNATURE_HEADERS:
        value = headers.get(name)
        if value:
            return value.strip()
    return ""


class IngestReceiver:
    """入站事件接收与路由"""

    def __init__(self, workspace_id: str = "default"):
        self.workspace_id = workspace_id
        self.secret = os.environ.get("INSFLOW_INGEST_SECRET", "")
        self.bus = EventBus(workspace_id)

    async def handle(self, raw_body: bytes, headers, signature: str) -> dict:
        """处理入站事件（验签 → 分发）"""
        if not self.secret:
            return {"ok": False, "error": "INSFLOW_INGEST_SECRET 未配置（fail-closed）"}

        if not verify_signature(raw_body, signature, self.secret):
            self.bus.emit("ingest.rejected", {"reason": "signature_mismatch"})
            return {"ok": False, "error": "签名校验失败"}

        try:
            payload = json.loads(raw_body.decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            return {"ok": False, "error": f"载荷解析失败: {e}"}

        event_type = payload.get("event") or payload.get("type") or ""
        self.bus.emit("ingest.received", {"event": event_type})

        handler = self._handlers().get(event_type)
        if not handler:
            # 未识别的事件只记录不处理（不拒绝——MFlow/OpenFlow 事件类型会持续增加）
            return {"ok": True, "ignored": True, "event": event_type}

        return await handler(payload)

    def _handlers(self) -> dict:
        return {
            "content.published": self._on_content_published,
            "content.failed": self._on_content_failed,
            # OpenFlow 钩子事件（零插件通道/官方插件共用 ingest）
            "cdp_event": self._on_generic_event,
            "lead": self._on_generic_event,
            "contact": self._on_generic_event,
        }

    # ========== MFlow 发布回流 ==========

    async def _on_content_published(self, payload: dict) -> dict:
        """content.published：关联 insight → 动作完成 → 进入验证状态机

        MFlow webhook 出站格式（1-4 Dev/scripts/publish_adapters/webhook.py）：
        POST item JSON：{item_id, topic, url?, title?, ...}
        IF 在 loop/create 时把 insight_id 记进 params/meta，此处按 item_id 关联回 action。
        """
        store = await get_store()
        tracker = FeedbackTracker(self.workspace_id)

        item_id = str(payload.get("item_id") or payload.get("loop_id") or "")
        insight_id = payload.get("insight_id") or ""
        url = payload.get("url", "")

        # 定位关联动作：优先 insight_id，其次按 ref 里带 item_id 的动作
        action = None
        if insight_id:
            actions = await store.list_actions(self.workspace_id, insight_id=str(insight_id))
            action = next((a for a in actions if a.state in ("dispatched", "pending")), None)
        if action is None and item_id:
            all_actions = await store.list_actions(self.workspace_id)
            action = next(
                (a for a in all_actions
                 if f"mflow:item:{item_id}" in (a.result_json.get("ref", "") + a.target_ref or "")
                 and a.state in ("dispatched", "pending")),
                None,
            )

        if action is None:
            self.bus.emit("ingest.unmatched", {"event": "content.published", "item_id": item_id})
            return {"ok": True, "matched": False, "detail": "未找到关联动作（已记录）"}

        # 动作完成 → 验证状态机（等 GSC/GA4 基线窗口）
        result = {"published_url": url, "item_id": item_id, "via": "mflow.webhook"}
        await tracker.mark_done(action, result)

        self.bus.emit("feedback.pipeline_started", {
            "action_id": action.id,
            "insight_id": action.insight_id,
            "url": url,
        })
        return {"ok": True, "matched": True, "action_id": action.id}

    async def _on_content_failed(self, payload: dict) -> dict:
        """产稿失败回流"""
        store = await get_store()
        tracker = FeedbackTracker(self.workspace_id)
        loop_id = str(payload.get("loop_id") or payload.get("item_id") or "")
        actions = await store.list_actions(self.workspace_id)
        action = next(
            (a for a in actions
             if f"mflow:loop:{loop_id}" in (a.result_json.get("ref", ""), a.target_ref or "")
             and a.state == "dispatched"),
            None,
        )
        if action:
            await tracker.mark_failed(action, payload.get("error", "mflow content failed"))
            return {"ok": True, "matched": True}
        return {"ok": True, "matched": False}

    # ========== OpenFlow 钩子事件（记录 + 旅程/漏斗数据底座）==========

    async def _on_generic_event(self, payload: dict) -> dict:
        """OpenFlow cdp_event/lead/contact → 记录事件流（M3 旅程重建用）"""
        self.bus.emit("external.event", {
            "kind": payload.get("event") or payload.get("type"),
            "data": payload,
        })
        return {"ok": True, "recorded": True}
