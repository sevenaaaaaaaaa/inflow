"""MFlow 动作适配器（AC-2）

- mflow.create_content：洞察 → MFlow loop/create 异步产稿（轮询 + 可选取稿）
- mflow.register_topic：洞察 → MFlow item/upsert + advance 状态机登记

铁律：IF 只创建 Loop 与草稿，绝不替用户点"发布"。
"""

from ..integrations.mflow import MFlowClient
from .router import ActionAdapter, ActionResult


class MFlowCreateContentAdapter(ActionAdapter):
    """mflow.create_content：一键生成 MFlow 草稿

    流程：login → loop/create（topic+brief 来自洞察）→ 轮询 → loop_detail
    可选 params_json.fetch_draft=true 时轮询完成后取稿回传。

    模板联动：洞察 stage_tags → MFlow 模板包（如 saas-growth.json）
    """

    def __init__(self, client: MFlowClient | None = None):
        self.client = client or MFlowClient()

    @property
    def action_type(self) -> str:
        return "mflow.create_content"

    async def execute(self, action: dict, ctx) -> dict:
        if not self.client.configured:
            return ActionResult.fail("MFLOW_BASE_URL / MFLOW_CONSOLE_PASSWORD 未配置")

        params = action.get("params_json", {})
        topic = params.get("topic") or action.get("title") or "Insight Flow 选题"
        brief = params.get("brief") or action.get("summary") or action.get("description", "")
        template_id = params.get("template_id")
        max_rounds = params.get("max_rounds", 3)

        try:
            created = await self.client.create_loop(
                topic=topic,
                brief=brief,
                template_id=template_id,
                max_rounds=max_rounds,
                item_id=params.get("item_id"),
            )
            if not created.get("ok", False):
                return ActionResult.fail(f"MFlow loop/create 失败: {created}")

            loop_id = created.get("id", "")
            ref = f"mflow:loop:{loop_id}"

            # 轮询等待完成（可配置跳过，批量场景可先登记再轮询）
            detail = await self.client.poll_until_done(
                loop_id,
                max_polls=params.get("max_polls", 10),
                interval_seconds=params.get("poll_interval", 2.0),
            )
            status = detail.get("status", "unknown")

            detail_str = f"loop={loop_id} status={status}"

            # 可选取稿
            draft_path = detail.get("draft_path") or detail.get("output_path") or ""
            if params.get("fetch_draft") and draft_path:
                try:
                    draft = await self.client.read_file(draft_path)
                    detail_str += f" draft_chars={len(draft)}"
                except Exception as e:
                    detail_str += f" fetch_failed={type(e).__name__}"

            return ActionResult.ok(ref=ref, detail=detail_str)
        except Exception as e:
            return ActionResult.fail(f"{type(e).__name__}: {e}")


class MFlowRegisterTopicAdapter(ActionAdapter):
    """mflow.register_topic：登记选题进 MFlow 状态机（不产稿）"""

    def __init__(self, client: MFlowClient | None = None):
        self.client = client or MFlowClient()

    @property
    def action_type(self) -> str:
        return "mflow.register_topic"

    async def execute(self, action: dict, ctx) -> dict:
        if not self.client.configured:
            return ActionResult.fail("MFLOW_BASE_URL / MFLOW_CONSOLE_PASSWORD 未配置")

        params = action.get("params_json", {})
        item = {
            "topic": params.get("topic") or action.get("title") or "Insight Flow 选题",
            "brief": params.get("brief") or action.get("summary") or action.get("description", ""),
            "source": "insight-flow",
            "insight_id": ctx.insight_id if hasattr(ctx, "insight_id") else "",
            **{k: v for k, v in params.items() if k.startswith("meta.")},
        }

        try:
            result = await self.client.upsert_item(item)
            if not result.get("ok", False):
                return ActionResult.fail(f"MFlow item/upsert 失败: {result}")
            item_id = result.get("item_id") or result.get("id") or ""
            # 推进到排程队列
            advance = await self.client.advance_item(str(item_id))
            ref = f"mflow:item:{item_id}"
            return ActionResult.ok(ref=ref, detail=f"upsert+advance ok advance={bool(advance.get('ok'))}")
        except Exception as e:
            return ActionResult.fail(f"{type(e).__name__}: {e}")
