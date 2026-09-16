"""MFlow 洞察信号源插件

将 Insight Flow 的洞察推送到 MFlow 的信号选题流中。
"""

from datetime import UTC, datetime

import httpx

from insflow.collectors.base import CollectContext, CollectResult, SourcePlugin


class MFlowSourcePlugin(SourcePlugin):
    """MFlow 洞察信号源插件

    将洞察推送到 MFlow 的 source 信号流中，
    让 MFlow 可以消费洞察并触发内容创建。
    """

    @property
    def id(self) -> str:
        return "mflow-source"

    @property
    def name(self) -> str:
        return "MFlow 洞察信号源"

    @property
    def capabilities(self) -> dict:
        return {
            "metrics": ["insight_signal"],
            "engines": ["mflow"],
            "latency": "standard",
            "cost_hint": "免费（本机通信）",
        }

    async def collect(self, ctx: CollectContext) -> CollectResult:
        """采集洞察信号（从 Insight Flow 读取）"""
        # 这个插件主要是推送，collect 返回空
        return CollectResult(
            source=self.id,
            kind="insight_signal",
            items=[],
            cost={"units": 0, "currency": "USD"},
        )

    async def push_insight(self, insight: dict, config: dict) -> dict:
        """推送洞察到 MFlow

        Args:
            insight: 洞察数据
            config: MFlow 配置

        Returns:
            MFlow 响应
        """
        base_url = config.get("mflow_base_url", "http://localhost:8088")
        password = config.get("mflow_password", "")

        # MFlow source 插件信号格式
        signal = {
            "source": "insight-flow",
            "kind": "insight",
            "title": insight.get("title", ""),
            "summary": insight.get("summary", ""),
            "severity": insight.get("severity", "medium"),
            "confidence": insight.get("confidence", 0.5),
            "tags": insight.get("stage_tags_json", []),
            "actions": insight.get("actions_json", []),
            "captured_at": datetime.now(UTC).isoformat(),
        }

        # 推送到 MFlow 的 source 信号接口
        headers = {
            "Content-Type": "application/json",
            "X-MFlow-Password": password,
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{base_url}/api/source/signal",
                json=signal,
                headers=headers,
                timeout=30.0,
            )
            resp.raise_for_status()
            return resp.json()

    async def check_health(self, config: dict) -> bool:
        """健康检查"""
        try:
            base_url = config.get("mflow_base_url", "http://localhost:8088")
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{base_url}/health", timeout=5.0)
                return resp.status_code == 200
        except Exception:
            return False

    def validate_config(self, config: dict) -> list[str]:
        errors = []
        if not config.get("mflow_base_url"):
            errors.append("mflow_base_url is required")
        if not config.get("mflow_password"):
            errors.append("mflow_password is required")
        return errors


def create_plugin() -> SourcePlugin:
    """插件工厂函数"""
    return MFlowSourcePlugin()
