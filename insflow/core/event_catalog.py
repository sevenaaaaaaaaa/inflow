"""Insight Flow 事件目录（机器可读 + 文档同源）

伙伴对接只认这一份：`GET /api/v1/events/catalog` 与 `docs/14-事件目录.md`
都从本模块生成。手改文档会在 CI 被判过期。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

SCHEMA_VERSION = "1.0"
REPO_ROOT = Path(__file__).resolve().parents[2]
DOC_PATH = REPO_ROOT / "docs" / "14-事件目录.md"

DIRECTIONS = ("inbound", "outbound", "internal")


@dataclass
class EventSpec:
    """一条可对外说明的事件。"""

    type: str
    direction: str
    channel: str
    description: str
    required: list[str]
    optional: list[str] = field(default_factory=list)
    example: dict = field(default_factory=dict)
    aliases: list[str] = field(default_factory=list)
    auth: str = ""
    idempotent: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


# 入站信封（docs/12 事件契约）
INBOUND_ENVELOPE = {
    "auth": "HMAC-SHA256 over raw body",
    "headers": ["X-IF-Signature", "X-Inbound-Signature"],
    "secret_env": "INSFLOW_INGEST_SECRET",
    "idempotency": "(workspace_id, source, event_id)",
    "common_fields": [
        "schema_version", "event|type", "occurred_at", "source",
        "event_id", "workspace_id", "payload|data",
    ],
    "endpoint": "POST /api/v1/ingest?workspace_id=<ws>",
}

EVENTS: list[EventSpec] = [
    # ---------- inbound ----------
    EventSpec(
        type="content.published",
        direction="inbound",
        channel="POST /api/v1/ingest",
        description="MFlow 发布成功回流：关联洞察动作 → 进入 14 天验证窗口。",
        required=["event", "event_id"],
        optional=["item_id", "loop_id", "insight_id", "url", "title", "source"],
        example={
            "schema_version": "1.0",
            "event": "content.published",
            "event_id": "pub-20260920-1",
            "source": "mflow",
            "item_id": "42",
            "insight_id": "ins_abc",
            "url": "https://example.com/posts/pricing-watch",
            "occurred_at": "2026-09-20T10:00:00+00:00",
        },
        auth="HMAC-SHA256 (X-IF-Signature)",
        idempotent=True,
    ),
    EventSpec(
        type="content.failed",
        direction="inbound",
        channel="POST /api/v1/ingest",
        description="MFlow 产稿失败回流：匹配 mflow:loop:* 动作并 mark_failed。",
        required=["event", "event_id"],
        optional=["loop_id", "item_id", "error", "source"],
        example={
            "event": "content.failed",
            "event_id": "fail-20260920-1",
            "source": "mflow",
            "loop_id": "8f3a",
            "error": "template missing",
        },
        auth="HMAC-SHA256 (X-IF-Signature)",
        idempotent=True,
    ),
    EventSpec(
        type="cdp.event",
        direction="inbound",
        channel="POST /api/v1/ingest",
        description="OpenFlow 通用行为事件：有 identity 则写入 journey_events。",
        required=["event", "event_id"],
        optional=["source", "occurred_at", "data", "payload"],
        aliases=["cdp_event"],
        example={
            "event": "cdp.event",
            "event_id": "evt-1001",
            "source": "openflow",
            "data": {
                "identity": "user@example.com",
                "stage": "visit",
                "channel": "search",
                "occurred_at": "2026-09-20T10:00:00+00:00",
            },
        },
        auth="HMAC-SHA256 (X-IF-Signature)",
        idempotent=True,
    ),
    EventSpec(
        type="cdp.lead",
        direction="inbound",
        channel="POST /api/v1/ingest",
        description="线索事件：journey_events + 一方指标 first_party_leads。",
        required=["event", "event_id"],
        optional=["source", "data", "payload"],
        aliases=["lead"],
        example={
            "event": "cdp.lead",
            "event_id": "lead-88",
            "source": "openflow",
            "data": {"email": "lead@example.com", "channel": "ads", "campaign": "q3"},
        },
        auth="HMAC-SHA256 (X-IF-Signature)",
        idempotent=True,
    ),
    EventSpec(
        type="cdp.contact",
        direction="inbound",
        channel="POST /api/v1/ingest",
        description="联系人事件：与 cdp.lead 同样落一方线索指标。",
        required=["event", "event_id"],
        optional=["source", "data", "payload"],
        aliases=["contact"],
        example={
            "event": "cdp.contact",
            "event_id": "ct-12",
            "source": "openflow",
            "data": {"email": "ops@example.com", "channel": "referral"},
        },
        auth="HMAC-SHA256 (X-IF-Signature)",
        idempotent=True,
    ),
    EventSpec(
        type="cdp.order",
        direction="inbound",
        channel="POST /api/v1/ingest",
        description="成交事件：journey_events + order_count / order_revenue。",
        required=["event", "event_id"],
        optional=["source", "data", "payload"],
        aliases=["order"],
        example={
            "event": "cdp.order",
            "event_id": "ord-501",
            "source": "openflow",
            "data": {
                "identity": "buyer@example.com",
                "amount": 199.0,
                "channel": "search",
                "campaign": "brand",
            },
        },
        auth="HMAC-SHA256 (X-IF-Signature)",
        idempotent=True,
    ),
    # ---------- outbound ----------
    EventSpec(
        type="webhook.generic",
        direction="outbound",
        channel="HTTP POST target_ref（ActionRouter）",
        description="通用出站 Webhook。载荷 event=insight.action，可选 HMAC。",
        required=["action_type", "target_ref"],
        optional=["params_json.url", "params_json.secret", "params_json.data"],
        example={
            "event": "insight.action",
            "workspace_id": "ws_1",
            "insight_id": "ins_abc",
            "action": "webhook.generic",
            "description": "同步到 n8n",
            "data": {"severity": "high"},
            "ts": "2026-09-20T10:00:00+00:00",
        },
        auth="HMAC-SHA256 (X-IF-Signature)，密钥 INSFLOW_WEBHOOK_SECRET 或 params.secret",
        idempotent=False,
    ),
    EventSpec(
        type="openflow.webhook_insight",
        direction="outbound",
        channel="OpenFlow POST /api/webhook.php",
        description="洞察落入 OpenFlow CDP（零插件通道）。",
        required=["action_type", "target_ref|connector_id"],
        optional=["title", "summary", "severity", "params_json"],
        example={
            "id": "ins_abc",
            "workspace_id": "ws_1",
            "type": "competitor_pricing",
            "title": "竞品降价",
            "summary": "定价页 -12%",
            "severity": "high",
            "confidence": 0.8,
        },
        auth="OPENFLOW_WEBHOOK_SECRET",
        idempotent=False,
    ),
    EventSpec(
        type="openflow.automation",
        direction="outbound",
        channel="OpenFlow inbound / 插件路由",
        description="模型建议的自动化动作。载荷 event=if.automation_request。",
        required=["action_type"],
        optional=["target_ref", "params_json.automation", "params_json.connector_id"],
        example={
            "event": "if.automation_request",
            "visitor_id": "ws_1",
            "automation": "nurture-lead",
            "insight_id": "ins_abc",
            "params": {},
            "source_system": "insight-flow",
        },
        auth="OPENFLOW_WEBHOOK_SECRET 或 OPENFLOW_PLUGIN_TOKEN",
        idempotent=False,
    ),
    EventSpec(
        type="openflow.plugin_api",
        direction="outbound",
        channel="OpenFlow /api/plugin/insight-flow/<route>",
        description="官方插件专用通道。",
        required=["action_type"],
        optional=["target_ref", "params_json.route"],
        example={"route": "generic", "workspace_id": "ws_1", "insight_id": "ins_abc"},
        auth="X-Insight-Flow-Signature + 可选 Bearer",
        idempotent=False,
    ),
    EventSpec(
        type="mflow.create_content",
        direction="outbound",
        channel="MFlow POST /api/loop/create",
        description="一键产稿。IF 绝不代用户发布；发布结果走 content.published 回流。",
        required=["action_type"],
        optional=["params_json.template_id", "params_json.fetch_draft"],
        example={"action_type": "mflow.create_content", "insight_id": "ins_abc"},
        auth="MFlow 控制台 session（保险库存密码）",
        idempotent=False,
    ),
    EventSpec(
        type="mflow.register_topic",
        direction="outbound",
        channel="MFlow /api/item/upsert + advance",
        description="把洞察登记进 MFlow 选题状态机，不产稿。",
        required=["action_type"],
        optional=["params_json"],
        example={"action_type": "mflow.register_topic", "insight_id": "ins_abc"},
        auth="MFlow 控制台 session",
        idempotent=False,
    ),
    EventSpec(
        type="feishu.notify",
        direction="outbound",
        channel="飞书自定义机器人 webhook",
        description="飞书告警/日报出站。",
        required=["action_type", "target_ref|params_json.url"],
        optional=["params_json.secret", "title", "summary"],
        example={"action_type": "feishu.notify", "target_ref": "https://open.feishu.cn/open-apis/bot/v2/hook/xxx"},
        auth="可选机器人签名 secret",
        idempotent=False,
    ),
    EventSpec(
        type="slack.notify",
        direction="outbound",
        channel="Slack incoming webhook",
        description="Slack 出站通知。",
        required=["action_type", "target_ref|params_json.url"],
        optional=["title", "summary"],
        example={"action_type": "slack.notify", "target_ref": "https://hooks.slack.com/services/T/B/X"},
        auth="Webhook URL 即凭据",
        idempotent=False,
    ),
    EventSpec(
        type="email.send",
        direction="outbound",
        channel="SMTP",
        description="邮件通知（stdlib smtplib）。",
        required=["action_type"],
        optional=["params_json.to", "SMTP_HOST"],
        example={"action_type": "email.send", "params_json": {"to": "ops@example.com"}},
        auth="SMTP_USER / SMTP_PASSWORD",
        idempotent=False,
    ),
    # ---------- internal（集成状态页/审计可见）----------
    EventSpec(
        type="ingest.rejected",
        direction="internal",
        channel="EventBus",
        description="入站验签失败。",
        required=["reason"],
        example={"reason": "signature_mismatch"},
    ),
    EventSpec(
        type="ingest.duplicate",
        direction="internal",
        channel="EventBus",
        description="同一 (source, event_id) 重放，已短路。",
        required=["event", "event_id"],
        example={"event": "cdp.order", "event_id": "ord-501"},
    ),
    EventSpec(
        type="ingest.unmatched",
        direction="internal",
        channel="EventBus",
        description="content.published 找不到关联动作。",
        required=["event"],
        optional=["item_id"],
        example={"event": "content.published", "item_id": "42"},
    ),
    EventSpec(
        type="action.proposed",
        direction="internal",
        channel="EventBus",
        description="Agent/MCP 起草待审批动作（未派发）。",
        required=["action_id"],
        optional=["insight_id", "proposed_by"],
        example={"action_id": "act_1", "proposed_by": "agent"},
    ),
    EventSpec(
        type="action.dispatched",
        direction="internal",
        channel="EventBus",
        description="动作已派发到适配器。",
        required=["action_type"],
        optional=["insight_id", "action_id"],
        example={"action_type": "webhook.generic", "insight_id": "ins_abc"},
    ),
    EventSpec(
        type="action.dead",
        direction="internal",
        channel="EventBus",
        description="重试耗尽，进入死信（可人工重放）。",
        required=["action_id"],
        optional=["retries"],
        example={"action_id": "act_1", "retries": 3},
    ),
    EventSpec(
        type="feedback.received",
        direction="internal",
        channel="EventBus",
        description="14 天验证窗口产出结论。",
        required=["action_id"],
        optional=["effective"],
        example={"action_id": "act_1", "effective": True},
    ),
    EventSpec(
        type="insight.created",
        direction="internal",
        channel="EventBus",
        description="质量门通过后洞察出闸。",
        required=["insight_id"],
        optional=["type"],
        example={"insight_id": "ins_abc", "type": "competitor_pricing"},
    ),
    EventSpec(
        type="quota.exceeded",
        direction="internal",
        channel="EventBus",
        description="配额熔断（采集/Agent 停写）。",
        required=["source"],
        optional=["limit"],
        example={"source": "serper", "limit": 20000},
    ),
    EventSpec(
        type="source.token_refresh_failed",
        direction="internal",
        channel="EventBus",
        description="OAuth 轮换失败，需重新授权。",
        required=["source"],
        example={"source": "ga4"},
    ),
]


def catalog(direction: str | None = None) -> dict:
    """API 响应：信封 + 事件列表。"""
    items = EVENTS
    if direction:
        if direction not in DIRECTIONS:
            raise ValueError(f"direction 必须是 {DIRECTIONS}")
        items = [e for e in EVENTS if e.direction == direction]
    return {
        "schema_version": SCHEMA_VERSION,
        "envelope": {"inbound": INBOUND_ENVELOPE},
        "count": len(items),
        "events": [e.to_dict() for e in items],
    }


def lookup(event_type: str) -> EventSpec | None:
    """按 type 或 alias 查找。"""
    for spec in EVENTS:
        if spec.type == event_type or event_type in spec.aliases:
            return spec
    return None


def render_markdown() -> str:
    """生成 docs/14-事件目录.md（确定性，供 CI 对拍）。"""
    lines = [
        "# Insight Flow 事件目录",
        "",
        "> 本文件由 `insflow events catalog --write` 从"
        " `insflow/core/event_catalog.py` 生成，不要手改。",
        f"> schema_version = {SCHEMA_VERSION}",
        "",
        "机器可读：`GET /api/v1/events/catalog`；可加 `?direction=inbound|outbound|internal`。",
        "",
        "## 入站信封",
        "",
        f"- 端点：`{INBOUND_ENVELOPE['endpoint']}`",
        f"- 验签：{INBOUND_ENVELOPE['auth']}，头 "
        f"`{'` / `'.join(INBOUND_ENVELOPE['headers'])}`",
        f"- 密钥：`{INBOUND_ENVELOPE['secret_env']}`（未配置 fail-closed）",
        f"- 幂等：`{INBOUND_ENVELOPE['idempotency']}`，重放返回 `duplicate`",
        f"- 公共字段：{', '.join(f'`{f}`' for f in INBOUND_ENVELOPE['common_fields'])}",
        "",
    ]
    for direction in DIRECTIONS:
        title = {"inbound": "入站（伙伴 → IF）",
                 "outbound": "出站（IF → 伙伴）",
                 "internal": "内部（EventBus / 集成状态页）"}[direction]
        lines += [f"## {title}", ""]
        for spec in (e for e in EVENTS if e.direction == direction):
            alias = f" （别名：{', '.join(spec.aliases)}）" if spec.aliases else ""
            lines += [
                f"### `{spec.type}`{alias}",
                "",
                spec.description,
                "",
                f"- 方向：`{spec.direction}` · 通道：{spec.channel}",
                f"- 必填：{', '.join(f'`{x}`' for x in spec.required) or '—'}",
                f"- 可选：{', '.join(f'`{x}`' for x in spec.optional) or '—'}",
            ]
            if spec.auth:
                lines.append(f"- 鉴权：{spec.auth}")
            if spec.idempotent:
                lines.append("- 幂等：是")
            lines += [
                "",
                "```json",
                json.dumps(spec.example, ensure_ascii=False, indent=2),
                "```",
                "",
            ]
    return "\n".join(lines)


def write_docs(path: Path | None = None) -> Path:
    dest = path or DOC_PATH
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(render_markdown(), encoding="utf-8")
    return dest


def ingest_handler_types() -> set[str]:
    """代码事实：IngestReceiver 已注册的事件名。"""
    from ..actions.ingest import IngestReceiver
    return set(IngestReceiver("_")._handlers())


def outbound_action_types() -> set[str]:
    """代码事实：内置 ActionRouter 类型（插件动作不进契约目录）。"""
    from ..actions.router import get_action_router
    return set(get_action_router().list_types(source="builtin"))


def catalog_inbound_names() -> set[str]:
    names: set[str] = set()
    for spec in EVENTS:
        if spec.direction != "inbound":
            continue
        names.add(spec.type)
        names.update(spec.aliases)
    return names


def catalog_outbound_names() -> set[str]:
    return {e.type for e in EVENTS if e.direction == "outbound"}
