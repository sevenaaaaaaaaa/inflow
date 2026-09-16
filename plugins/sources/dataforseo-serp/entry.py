"""DataForSEO SERP API 适配器"""

import httpx
import base64
from typing import Any

from insflow.collectors.base import CollectContext, CollectResult, SourcePlugin
from insflow.collectors.search import (
    SearchEngine,
    SearchProvider,
    SearchQuery,
    SearchResponse,
    SearchResult,
)


class DataForSEOProvider(SearchProvider):
    """DataForSEO SERP 提供者

    文档: https://docs.dataforseo.com/v3/serp/google/organic/task_post/
    """

    API_URL = "https://api.dataforseo.com/v3/serp/{engine}/organic/task_post"

    ENGINE_MAP = {
        SearchEngine.GOOGLE: "google",
        SearchEngine.BING: "bing",
    }

    @property
    def id(self) -> str:
        return "dataforseo-serp"

    @property
    def name(self) -> str:
        return "DataForSEO SERP API"

    @property
    def supported_engines(self) -> list[SearchEngine]:
        return [SearchEngine.GOOGLE, SearchEngine.BING]

    @property
    def cost_hint(self) -> str:
        return "0.0006 USD/SERP standard"

    def _get_auth_header(self, config: dict) -> str:
        """生成 Basic Auth header"""
        login = config.get("login", "")
        password = config.get("password", "")
        credentials = f"{login}:{password}"
        return base64.b64encode(credentials.encode()).decode()

    async def search(self, query: SearchQuery, config: dict) -> SearchResponse:
        """执行搜索"""
        login = config.get("login", "")
        password = config.get("password", "")
        if not login or not password:
            raise ValueError("DataForSEO login and password are required")

        engine = self.ENGINE_MAP.get(query.engine, "google")
        url = self.API_URL.format(engine=engine)

        headers = {
            "Authorization": f"Basic {self._get_auth_header(config)}",
            "Content-Type": "application/json",
        }

        # DataForSEO 使用 POST 请求
        payload = [{
            "keyword": query.query,
            "location_name": query.country.upper(),
            "language_name": query.language,
            "depth": query.num_results,
            "device": "desktop",
            "os": "windows",
        }]

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                json=payload,
                headers=headers,
                timeout=60.0,
            )
            resp.raise_for_status()
            data = resp.json()

        # 解析结果
        results = []
        tasks = data.get("tasks", [])
        if tasks and tasks[0].get("result"):
            for item in tasks[0]["result"][0].get("items", []):
                if item.get("type") == "organic":
                    results.append(SearchResult(
                        title=item.get("title", ""),
                        url=item.get("url", ""),
                        snippet=item.get("description", ""),
                        position=item.get("rank_group", 0),
                        domain=item.get("domain", ""),
                        metadata={
                            "serp_features": item.get("serp_features", []),
                        },
                    ))

        # 计算成本（标准优先级：$0.0006/请求）
        cost_per_request = 0.0006
        if query.extra.get("priority"):
            cost_per_request = 0.006  # 优先级模式

        return SearchResponse(
            query=query.query,
            engine=engine,
            results=results,
            total_results=tasks[0].get("result", [{}])[0].get("total_count", 0) if tasks else 0,
            cost={"units": cost_per_request, "currency": "USD"},
            raw=data,
        )

    def validate_config(self, config: dict) -> list[str]:
        errors = []
        if not config.get("login"):
            errors.append("login is required")
        if not config.get("password"):
            errors.append("password is required")
        return errors


class DataForSEOSSourcePlugin(SourcePlugin):
    """DataForSEO 数据源插件"""

    def __init__(self):
        self._provider = DataForSEOProvider()

    @property
    def id(self) -> str:
        return "dataforseo-serp"

    @property
    def name(self) -> str:
        return "DataForSEO SERP API"

    @property
    def capabilities(self) -> dict:
        return {
            "metrics": ["serp_organic", "serp_features", "ai_overview"],
            "engines": ["google", "bing", "youtube"],
            "latency": "standard|priority|live",
            "cost_hint": "0.0006 USD/SERP standard",
        }

    async def collect(self, ctx: CollectContext) -> CollectResult:
        """执行采集"""
        engine = ctx.config.get("engine", "google")
        engine_enum = SearchEngine.GOOGLE if engine == "google" else SearchEngine.BING

        query = SearchQuery(
            query=ctx.config.get("query", ""),
            engine=engine_enum,
            country=ctx.config.get("country", "us"),
            language=ctx.config.get("language", "en"),
            num_results=ctx.config.get("num_results", 10),
            extra={"priority": ctx.config.get("priority", False)},
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
    return DataForSEOSSourcePlugin()
