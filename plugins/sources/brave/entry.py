"""Brave Search API 适配器"""

import httpx
from typing import Any

from insflow.collectors.base import CollectContext, CollectResult, SourcePlugin
from insflow.collectors.search import (
    SearchEngine,
    SearchProvider,
    SearchQuery,
    SearchResponse,
    SearchResult,
)


class BraveProvider(SearchProvider):
    """Brave Search 提供者"""

    API_URL = "https://api.search.brave.com/res/v1/web/search"

    @property
    def id(self) -> str:
        return "brave"

    @property
    def name(self) -> str:
        return "Brave Search API"

    @property
    def supported_engines(self) -> list[SearchEngine]:
        return [SearchEngine.BRAVE]

    @property
    def cost_hint(self) -> str:
        return "Free tier: 2000 req/mo; $3/1000 queries"

    async def search(self, query: SearchQuery, config: dict) -> SearchResponse:
        """执行搜索"""
        api_key = config.get("api_key", "")
        if not api_key:
            raise ValueError("Brave API key is required")

        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": api_key,
        }

        params = {
            "q": query.query,
            "country": query.country,
            "search_lang": query.language,
            "count": min(query.num_results, 20),  # Brave 最多 20
            "offset": (query.page - 1) * query.num_results,
        }

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                self.API_URL,
                params=params,
                headers=headers,
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()

        # 解析结果
        results = []
        for i, item in enumerate(data.get("web", {}).get("results", [])):
            results.append(SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=item.get("description", ""),
                position=i + 1,
                domain=item.get("meta_url", {}).get("hostname", ""),
            ))

        total = data.get("query", {}).get("total_results", 0)

        return SearchResponse(
            query=query.query,
            engine="brave",
            results=results,
            total_results=total,
            cost={"units": 0.003, "currency": "USD"},
            raw=data,
        )

    def validate_config(self, config: dict) -> list[str]:
        errors = []
        if not config.get("api_key"):
            errors.append("api_key is required")
        return errors


class BraveSourcePlugin(SourcePlugin):
    """Brave 数据源插件"""

    def __init__(self):
        self._provider = BraveProvider()

    @property
    def id(self) -> str:
        return "brave"

    @property
    def name(self) -> str:
        return "Brave Search API"

    @property
    def capabilities(self) -> dict:
        return {
            "metrics": ["serp_organic", "serp_news", "serp_videos"],
            "engines": ["brave"],
            "latency": "priority",
            "cost_hint": "Free tier: 2000 req/mo; $3/1000 queries",
        }

    async def collect(self, ctx: CollectContext) -> CollectResult:
        """执行采集"""
        query = SearchQuery(
            query=ctx.config.get("query", ""),
            engine=SearchEngine.BRAVE,
            country=ctx.config.get("country", "us"),
            language=ctx.config.get("language", "en"),
            num_results=ctx.config.get("num_results", 10),
        )

        response = await self._provider.search(query, ctx.config)

        return CollectResult(
            source=self.id,
            kind="serp_organic",
            items=[r.__dict__ for r in response.results],
            cost=response.cost,
        )

    async def check_health(self, config: dict) -> bool:
        return await self._provider.check_health(config)

    def validate_config(self, config: dict) -> list[str]:
        return self._provider.validate_config(config)


def create_plugin() -> SourcePlugin:
    """插件工厂函数"""
    return BraveSourcePlugin()
