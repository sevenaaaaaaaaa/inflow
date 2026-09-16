"""Insight Flow MCP Server（stdio 传输）

10 个工具（完整版）：list_insights / get_insight / run_diagnosis / ask_analyst /
list_competitors / get_competitor_timeline / get_maturity / list_monitors /
trigger_playbook / get_feedback_stats

工具实现集中在 tools.py（与 HTTP 端点共用）。
消费方：OpenFlow AgentRuntime（mcp:*）、Claude / Cursor / 任意 MCP 客户端。
"""

import asyncio

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .tools import MCP_TOOLS_SCHEMA, call_mcp_tool

server = Server("insight-flow")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [Tool(
        name=t["name"],
        description=t["description"],
        inputSchema=t["inputSchema"],
    ) for t in MCP_TOOLS_SCHEMA]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    output = await call_mcp_tool(name, arguments)
    return [TextContent(type="text", text=output)]


async def run_server():
    """stdio 传输入口"""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main():
    asyncio.run(run_server())


if __name__ == "__main__":
    main()
