"""Insight Flow MCP Server

MCP Server 骨架（stdio 传输）
工具清单：list_insights / get_insight
"""

import asyncio
import json

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from ..core.store import get_store

# 创建 MCP Server 实例
server = Server("insight-flow")


@server.list_tools()
async def list_tools() -> list[Tool]:
    """列出可用工具"""
    return [
        Tool(
            name="list_insights",
            description="列出洞察。可按工作区、状态、严重程度过滤。",
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace_id": {
                        "type": "string",
                        "description": "工作区ID",
                    },
                    "status": {
                        "type": "string",
                        "description": "洞察状态过滤 (new/acknowledged/actioned/verified/dismissed)",
                        "enum": ["new", "acknowledged", "actioned", "verified", "dismissed"],
                    },
                    "severity": {
                        "type": "string",
                        "description": "严重程度过滤",
                        "enum": ["critical", "high", "medium", "low", "info"],
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回数量限制",
                        "default": 20,
                    },
                },
                "required": ["workspace_id"],
            },
        ),
        Tool(
            name="get_insight",
            description="获取洞察详情",
            inputSchema={
                "type": "object",
                "properties": {
                    "insight_id": {
                        "type": "string",
                        "description": "洞察ID",
                    },
                },
                "required": ["insight_id"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """调用工具"""
    store = await get_store()

    if name == "list_insights":
        workspace_id = arguments.get("workspace_id")
        status = arguments.get("status")
        severity = arguments.get("severity")
        limit = arguments.get("limit", 20)

        insights = await store.list_insights(
            workspace_id,
            status=status,
            severity=severity,
            limit=limit,
        )

        result = {
            "insights": [
                {
                    "id": ins.id,
                    "type": ins.type,
                    "title": ins.title,
                    "summary": ins.summary,
                    "severity": ins.severity.value,
                    "confidence": ins.confidence,
                    "status": ins.status.value,
                    "created_at": ins.created_at.isoformat(),
                }
                for ins in insights
            ],
            "total": len(insights),
        }

        return [TextContent(
            type="text",
            text=json.dumps(result, ensure_ascii=False, indent=2),
        )]

    elif name == "get_insight":
        insight_id = arguments.get("insight_id")
        insight = await store.get_insight(insight_id)

        if not insight:
            return [TextContent(
                type="text",
                text=json.dumps({"error": f"Insight not found: {insight_id}"}, ensure_ascii=False),
            )]

        result = {
            "id": insight.id,
            "type": insight.type,
            "title": insight.title,
            "summary": insight.summary,
            "severity": insight.severity.value,
            "confidence": insight.confidence,
            "evidence": insight.evidence_json,
            "actions": insight.actions_json,
            "stage_tags": insight.stage_tags_json,
            "status": insight.status.value,
            "created_at": insight.created_at.isoformat(),
            "verified_at": insight.verified_at.isoformat() if insight.verified_at else None,
        }

        return [TextContent(
            type="text",
            text=json.dumps(result, ensure_ascii=False, indent=2),
        )]

    else:
        return [TextContent(
            type="text",
            text=json.dumps({"error": f"Unknown tool: {name}"}, ensure_ascii=False),
        )]


async def run_server():
    """运行 MCP Server（stdio 传输）"""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main():
    """入口函数"""
    asyncio.run(run_server())


if __name__ == "__main__":
    main()
