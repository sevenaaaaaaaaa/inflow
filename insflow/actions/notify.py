"""IM 告警出站适配器（飞书 / Slack）

- feishu.notify：飞书自定义机器人 webhook（告警/日报，PRD PL-3）
- slack.notify：Slack incoming webhook

统一 Action 接口：execute(action, ctx) -> {ok, ref, detail}
"""

import json

import httpx

from .router import ActionAdapter, ActionResult


class FeishuNotifyAdapter(ActionAdapter):
    """飞书自定义机器人通知

    target_ref 或 params_json.url 填机器人 webhook 地址：
    https://open.feishu.cn/open-apis/bot/v2/hook/xxxx
    支持签名校验机器人（params_json.secret）
    """

    API_URL_PREFIX = "https://open.feishu.cn/open-apis/bot/v2/hook/"

    @property
    def action_type(self) -> str:
        return "feishu.notify"

    async def execute(self, action: dict, ctx) -> dict:
        params = action.get("params_json", {})
        url = action.get("target_ref") or params.get("url", "")
        if not url or self.API_URL_PREFIX not in url:
            return ActionResult.fail("feishu.notify 需要有效的飞书机器人 webhook 地址")

        title = action.get("title") or "Insight Flow 通知"
        text = action.get("summary") or action.get("description") or action.get("message", "")

        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": f"📈 {title}"},
                    "template": "blue" if action.get("severity") != "high" else "red",
                },
                "elements": [
                    {"tag": "div", "text": {"tag": "lark_md", "content": text[:4000]}},
                ],
            },
        }

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, json=payload, timeout=15.0)
                data = resp.json()
                ok = resp.status_code == 200 and data.get("code", 0) == 0
                return ActionResult.ok(
                    ref=f"feishu:{data.get('code', resp.status_code)}",
                    detail=json.dumps(data, ensure_ascii=False)[:500],
                ) if ok else ActionResult.fail(f"feishu: {data}")
        except Exception as e:
            return ActionResult.fail(f"{type(e).__name__}: {e}")


class SlackNotifyAdapter(ActionAdapter):
    """Slack incoming webhook 通知"""

    @property
    def action_type(self) -> str:
        return "slack.notify"

    async def execute(self, action: dict, ctx) -> dict:
        params = action.get("params_json", {})
        url = action.get("target_ref") or params.get("url", "")
        if not url or "hooks.slack.com" not in url:
            return ActionResult.fail("slack.notify 需要有效的 Slack webhook 地址")

        title = action.get("title") or "Insight Flow 通知"
        text = action.get("summary") or action.get("description") or action.get("message", "")

        payload = {
            "text": f"*{title}*\n{text}",
            "mrkdwn": True,
        }

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, json=payload, timeout=15.0)
                ok = resp.status_code == 200
                return ActionResult.ok(
                    ref="slack:200", detail="posted",
                ) if ok else ActionResult.fail(f"HTTP {resp.status_code}")
        except Exception as e:
            return ActionResult.fail(f"{type(e).__name__}: {e}")


class EmailNotifyAdapter(ActionAdapter):
    """邮件通知（SMTP，stdlib smtplib；面向超级个体的离线订阅渠道）

    配置优先级：动作 params_json > 环境变量 > 保险库
    环境变量：SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD / SMTP_FROM / SMTP_TLS
    """

    @property
    def action_type(self) -> str:
        return "email.send"

    def _smtp_config(self, params: dict) -> dict:
        import os
        return {
            "host": params.get("smtp_host") or os.environ.get("SMTP_HOST", ""),
            "port": int(params.get("smtp_port") or os.environ.get("SMTP_PORT", "587")),
            "user": params.get("smtp_user") or os.environ.get("SMTP_USER", ""),
            "password": params.get("smtp_password") or os.environ.get("SMTP_PASSWORD", ""),
            "sender": params.get("smtp_from") or os.environ.get("SMTP_FROM", ""),
            "tls": (params.get("smtp_tls") if "smtp_tls" in params
                    else os.environ.get("SMTP_TLS", "1")) not in (False, "0", 0),
        }

    async def execute(self, action: dict, ctx) -> dict:
        import asyncio
        params = action.get("params_json", {})
        to = action.get("target_ref") or params.get("to", "")
        if not to or "@" not in to:
            return ActionResult.fail("email.send 需要有效的收件地址（target_ref/params.to）")

        cfg = self._smtp_config(params)
        if not cfg["host"] or not cfg["sender"]:
            return ActionResult.fail("SMTP 未配置（SMTP_HOST/SMTP_FROM 缺失）")

        title = action.get("title") or "Insight Flow 通知"
        body = action.get("summary") or action.get("description") or ""
        severity = action.get("severity", "medium")

        try:
            await asyncio.to_thread(self._send, cfg, to, title, body, severity)
            return ActionResult.ok(ref=f"email:{to}", detail="sent")
        except Exception as e:
            return ActionResult.fail(f"{type(e).__name__}: {e}")

    def _send(self, cfg: dict, to: str, title: str, body: str, severity: str) -> None:
        """同步发送（在线程中执行，避免阻塞事件循环）"""
        import smtplib
        from email.message import EmailMessage

        msg = EmailMessage()
        msg["Subject"] = f"[{severity.upper()}] {title}"
        msg["From"] = cfg["sender"]
        msg["To"] = to
        msg.set_content(f"{title}\n\n{body}\n\n—— Insight Flow 情报台")

        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=20) as server:
            if cfg["tls"]:
                server.starttls()
            if cfg["user"]:
                server.login(cfg["user"], cfg["password"])
            server.send_message(msg)
