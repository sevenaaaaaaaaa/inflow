"""IF 作为 OpenFlow 的 MCP 客户端（M3 集成方案 §3.4）

消费 OpenFlow mcp-server.php 的数据类工具（只读 scope）：
- members_list / leads_count / orders_revenue / sentiment_topics / sentiment_scan

传输：MCP over HTTP（POST /mcp-server.php JSON-RPC）或本地 stdio（子进程）。
约束：只读——写操作一律经 OpenFlow 自己的 Agent 审批门，IF 不执行任何 OpenFlow 写工具。
"""

import json
import os

import httpx


class OpenFlowMCPError(Exception):
    pass


class OpenFlowMCPClient:
    """OpenFlow MCP 客户端（HTTP 传输）

    环境变量：
    - OPENFLOW_MCP_URL：MCP 端点（如 https://example.com/mcp-server.php）
    - OPENFLOW_MCP_API_KEY：McpGuard 多 Key 体系中的 Key
    未配置时 available=False（旅程重建/RFM 降级为手动数据模式）。
    """

    READONLY_TOOLS = {
        "members_list", "leads_count", "orders_revenue",
        "sentiment_topics", "articles_list", "search",
    }

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        self.base_url = (base_url or os.environ.get("OPENFLOW_MCP_URL", "")).rstrip("/")
        self.api_key = api_key or os.environ.get("OPENFLOW_MCP_API_KEY", "")
        self._request_id = 0

    @property
    def available(self) -> bool:
        return bool(self.base_url and self.api_key)

    async def call_tool(self, tool_name: str, arguments: dict | None = None) -> dict:
        """JSON-RPC tools/call（仅允许只读工具，fail-closed）"""
        if tool_name not in self.READONLY_TOOLS:
            raise OpenFlowMCPError(
                f"工具 {tool_name} 不在只读白名单中（IF 对 OpenFlow 只读，写操作经其审批门）"
            )
        if not self.available:
            raise OpenFlowMCPError("OPENFLOW_MCP_URL / OPENFLOW_MCP_API_KEY 未配置")

        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments or {}},
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self.base_url,
                json=payload,
                headers={
                    "X-API-Key": self.api_key,
                    "Content-Type": "application/json",
                },
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()

        if data.get("error"):
            raise OpenFlowMCPError(f"OpenFlow MCP 错误: {data['error']}")

        content = data.get("result", {}).get("content", [])
        if not content:
            return {}
        try:
            return json.loads(content[0].get("text", "{}"))
        except json.JSONDecodeError:
            return {"raw": content[0].get("text", "")}

    # ========== 第一方数据底座（CJ-3/CJ-5 数据源）==========

    async def get_members(self) -> list[dict]:
        """会员列表（RFM 的 R/F/M 原始数据源之一）"""
        data = await self.call_tool("members_list")
        if isinstance(data, list):
            return data
        return data.get("members", data.get("data", []))

    async def get_leads_summary(self) -> dict:
        """线索统计"""
        return await self.call_tool("leads_count")

    async def get_orders_summary(self) -> dict:
        """订单收入统计（RFM 金额维度）"""
        return await self.call_tool("orders_revenue")

    async def get_sentiment_topics(self) -> list[dict]:
        """舆情主题（旅程 See 阶段需求信号）"""
        data = await self.call_tool("sentiment_topics")
        if isinstance(data, list):
            return data
        return data.get("topics", [])

    async def collect_first_party_snapshot(self) -> dict:
        """拉取第一方数据快照（旅程重建/RFM/LTV:CAC 的底座）"""
        members = await self.get_members()
        leads = await self.get_leads_summary()
        orders = await self.get_orders_summary()
        return {
            "members": members,
            "leads": leads,
            "orders": orders,
            "fetched_at": None,  # 由调用方补充时间戳
        }
