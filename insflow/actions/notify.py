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
