"""知乎数据开放平台适配器

文档: https://open.zhihu.com/
需要申请开放平台 API Key

注：知乎开放平台限制较多，搜索接口配额为 1000 次/天
如需更高配额，建议使用新榜/清博等数据商
"""

import httpx
from datetime import datetime, timezone
from typing import Any

from insflow.collectors.base import CollectContext, CollectResult, SourcePlugin


class ZhihuProvider:
    """知乎数据开放平台提供者"""

    BASE_URL = "https://api.zhihu.com"

    async def search(self, query: str, search_type: str = "general",
                     limit: int = 20, config: dict = None) -> dict:
        """搜索知乎内容

        Args:
            query: 搜索查询
            search_type: 搜索类型 (general / article / answer / question)
            limit: 结果数量
            config: 配置
        """
        api_key = (config or {}).get("api_key", "")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "InsightFlow/1.0",
        }

        # 搜索接口
        url = f"{self.BASE_URL}/search_v3"
        params = {
            "q": query,
            "type": search_type,
            "limit": limit,
        }

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url,
                params=params,
                headers=headers,
                timeout=30.0,
            )
            resp.raise_for_status()
            return resp.json()

    async def get_question(self, question_id: str, config: dict = None) -> dict:
        """获取问题详情"""
        api_key = (config or {}).get("api_key", "")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "InsightFlow/1.0",
        }

        url = f"{self.BASE_URL}/questions/{question_id}"

        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=headers, timeout=30.0)
            resp.raise_for_status()
            return resp.json()

    async def get_hot_topics(self, config: dict = None) -> dict:
        """获取热门话题"""
        api_key = (config or {}).get("api_key", "")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "InsightFlow/1.0",
        }

        url = f"{self.BASE_URL}/topstory/hot-lists/total"

        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=headers, timeout=30.0)
            resp.raise_for_status()
            return resp.json()


class ZhihuSourcePlugin(SourcePlugin):
    """知乎数据源插件"""

    def __init__(self):
        self._provider = ZhihuProvider()

    @property
    def id(self) -> str:
        return "zhihu"

    @property
    def name(self) -> str:
        return "知乎数据开放平台"

    @property
    def capabilities(self) -> dict:
        return {
            "metrics": ["zhihu_questions", "zhihu_answers", "zhihu_articles"],
            "engines": ["zhihu"],
            "latency": "standard",
            "cost_hint": "免费档: 1000 次/天搜索",
        }

    async def collect(self, ctx: CollectContext) -> CollectResult:
        """执行采集"""
        query = ctx.config.get("query", "")
        search_type = ctx.config.get("search_type", "general")
        limit = ctx.config.get("limit", 20)

        data = await self._provider.search(
            query=query,
            search_type=search_type,
            limit=limit,
            config=ctx.config,
        )

        # 解析结果
        items = []
        for item in data.get("data", []):
            target = item.get("target", {})
            item_type = item.get("type", "")

            if item_type == "answer":
                items.append({
                    "type": "answer",
                    "id": target.get("id", ""),
                    "title": target.get("question", {}).get("title", ""),
                    "content": target.get("excerpt", ""),
                    "author": target.get("author", {}).get("name", ""),
                    "voteup_count": target.get("voteup_count", 0),
                    "comment_count": target.get("comment_count", 0),
                    "url": target.get("url", ""),
                    "created_time": target.get("created_time", 0),
                })
            elif item_type == "article":
                items.append({
                    "type": "article",
                    "id": target.get("id", ""),
                    "title": target.get("title", ""),
                    "content": target.get("excerpt", ""),
                    "author": target.get("author", {}).get("name", ""),
                    "voteup_count": target.get("voteup_count", 0),
                    "comment_count": target.get("comment_count", 0),
                    "url": target.get("url", ""),
                    "created": target.get("created", 0),
                })

        return CollectResult(
            source=self.id,
            kind="zhihu_search",
            items=items,
            cost={"units": 0, "currency": "USD"},  # 免费档
            metadata={"query": query, "type": search_type, "total": len(items)},
        )

    async def check_health(self, config: dict) -> bool:
        """健康检查"""
        try:
            await self._provider.get_hot_topics(config)
            return True
        except Exception:
            return False

    def validate_config(self, config: dict) -> list[str]:
        errors = []
        if not config.get("api_key"):
            errors.append("api_key is required")
        return errors


def create_plugin() -> SourcePlugin:
    """插件工厂函数"""
    return ZhihuSourcePlugin()
