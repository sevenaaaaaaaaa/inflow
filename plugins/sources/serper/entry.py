"""Serper.dev Google SERP API 适配器"""


import httpx

from insflow.collectors.base import CollectContext, CollectResult, SourcePlugin
from insflow.collectors.search import (
    SearchEngine,
    SearchProvider,
    SearchQuery,
    SearchResponse,
    SearchResult,
)


class SerperProvider(SearchProvider):
    """Serper.dev 搜索提供者"""

    API_URL = "https://google.serper.dev/search"

    @property
    def id(self) -> str:
        return "serper"

    @property
    def name(self) -> str:
        return "Serper.dev Google SERP API"

    @property
    def supported_engines(self) -> list[SearchEngine]:
        return [SearchEngine.GOOGLE]

    @property
    def cost_hint(self) -> str:
        return "0.001 USD/query (standard)"

    async def search(self, query: SearchQuery, config: dict) -> SearchResponse:
        """执行搜索"""
        api_key = config.get("api_key", "")
        if not api_key:
            raise ValueError("Serper API key is required")

        headers = {
            "X-API-KEY": api_key,
            "Content-Type": "application/json",
        }

        payload = {
            "q": query.query,
            "gl": query.country,
            "hl": query.language,
            "num": query.num_results,
            "page": query.page,
        }

        if query.date_from:
            payload["tbs"] = f"cdr:1,cd_min:{query.date_from},cd_max:{query.date_to or ''}"

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self.API_URL,
                json=payload,
                headers=headers,
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()

        # 解析结果
        results = []
        for i, item in enumerate(data.get("organic", [])):
            results.append(SearchResult(
                title=item.get("title", ""),
                url=item.get("link", ""),
                snippet=item.get("snippet", ""),
                position=i + 1,
                domain=item.get("domain", ""),
            ))

        return SearchResponse(
            query=query.query,
            engine="google",
            results=results,
            total_results=data.get("searchParameters", {}).get("totalResults", 0),
            cost={"units": 0.001, "currency": "USD"},
            raw=data,
        )

    def validate_config(self, config: dict) -> list[str]:
        errors = []
        if not config.get("api_key"):
            errors.append("api_key is required")
        return errors


class SerperSourcePlugin(SourcePlugin):
    """Serper 数据源插件（适配 SourcePlugin 接口）"""

    def __init__(self):
        self._provider = SerperProvider()

    @property
    def id(self) -> str:
        return "serper"

    @property
    def name(self) -> str:
        return "Serper.dev Google SERP API"

    @property
    def capabilities(self) -> dict:
        return {
            "metrics": ["serp_organic", "serp_features", "serp_local", "serp_news"],
            "engines": ["google"],
            "latency": "priority",
            "cost_hint": "0.001 USD/query",
        }

    async def collect(self, ctx: CollectContext) -> CollectResult:
        """执行采集"""
        query = SearchQuery(
            query=ctx.config.get("query", ""),
            engine=SearchEngine.GOOGLE,
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
    return SerperSourcePlugin()
