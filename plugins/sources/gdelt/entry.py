"""GDELT 适配器 - 全球事件、语言和语调数据库

文档: https://blog.gdeltproject.org/gdelt-geo-2-0-api-search/
免费 API，无需认证，但有速率限制
"""


import httpx

from insflow.collectors.base import CollectContext, CollectResult, SourcePlugin


class GDELTProvider:
    """GDELT 提供者"""

    # GDELT Geo 2.0 API
    GEO_API_URL = "https://api.gdeltproject.org/api/v2/geo/geo"
    # GDELT Context 2.0 API（新闻搜索）
    CONTEXT_API_URL = "https://api.gdeltproject.org/api/v2/doc/doc"

    @property
    def id(self) -> str:
        return "gdelt"

    @property
    def name(self) -> str:
        return "GDELT Global Database"

    async def search_news(self, query: str, mode: str = "artlist", max_records: int = 50) -> dict:
        """搜索新闻文章

        Args:
            query: 搜索查询
            mode: 模式 (artlist / artgallery / timeline / tonechart)
            max_records: 最大记录数
        """
        params = {
            "query": query,
            "mode": mode,
            "maxrecords": max_records,
            "format": "json",
        }

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                self.CONTEXT_API_URL,
                params=params,
                timeout=30.0,
            )
            resp.raise_for_status()
            return resp.json()

    async def search_geo(self, query: str, format: str = "GeoJSON") -> dict:
        """地理搜索"""
        params = {
            "query": query,
            "format": format,
        }

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                self.GEO_API_URL,
                params=params,
                timeout=30.0,
            )
            resp.raise_for_status()
            return resp.json()


class GDELTSourcePlugin(SourcePlugin):
    """GDELT 数据源插件"""

    def __init__(self):
        self._provider = GDELTProvider()

    @property
    def id(self) -> str:
        return "gdelt"

    @property
    def name(self) -> str:
        return "GDELT Global Database"

    @property
    def capabilities(self) -> dict:
        return {
            "metrics": ["news_mentions", "global_events", "tone_analysis"],
            "engines": ["gdelt"],
            "latency": "standard",
            "cost_hint": "Free (rate limited)",
        }

    async def collect(self, ctx: CollectContext) -> CollectResult:
        """执行采集"""
        query = ctx.config.get("query", "")
        mode = ctx.config.get("mode", "artlist")
        max_records = ctx.config.get("max_records", 50)

        data = await self._provider.search_news(query, mode, max_records)

        # 解析文章列表
        items = []
        for article in data.get("articles", []):
            items.append({
                "title": article.get("title", ""),
                "url": article.get("url", ""),
                "source": article.get("domain", ""),
                "language": article.get("language", ""),
                "tone": article.get("tone", 0),
                "date": article.get("seendate", ""),
                "socialimage": article.get("socialimage", ""),
            })

        return CollectResult(
            source=self.id,
            kind="news_mentions",
            items=items,
            cost={"units": 0, "currency": "USD"},  # 免费
            metadata={"mode": mode, "total": len(items)},
        )

    async def check_health(self, config: dict) -> bool:
        """健康检查"""
        try:
            await self._provider.search_news("test", max_records=1)
            return True
        except Exception:
            return False


def create_plugin() -> SourcePlugin:
    """插件工厂函数"""
    return GDELTSourcePlugin()
