"""Firecrawl 网页抓取适配器

文档: https://docs.firecrawl.dev/
免费档 1000 credits（1 credit/页）
用于：竞品定价页/changelog 抓取 → 变更监控语义 diff
"""

import httpx

from insflow.collectors.base import CollectContext, CollectResult, SourcePlugin

API_BASE = "https://api.firecrawl.dev/v1"


class FirecrawlSourcePlugin(SourcePlugin):
    """Firecrawl 数据源插件"""

    @property
    def id(self) -> str:
        return "firecrawl"

    @property
    def name(self) -> str:
        return "Firecrawl"

    @property
    def capabilities(self) -> dict:
        return {
            "metrics": ["page_content", "page_snapshot"],
            "engines": ["firecrawl"],
            "latency": "standard",
            "cost_hint": "1 credit/页",
        }

    async def collect(self, ctx: CollectContext) -> CollectResult:
        """抓取单页 → markdown"""
        api_key = ctx.config.get("api_key", "")
        url = ctx.config.get("url", "")
        if not api_key or not url:
            raise ValueError("firecrawl: api_key 与 url 必填")

        formats = ctx.config.get("formats", ["markdown"])

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{API_BASE}/scrape",
                json={"url": url, "formats": formats},
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                timeout=60.0,
            )
            resp.raise_for_status()
            data = resp.json()

        doc = data.get("data", {})
        markdown = doc.get("markdown", "")
        metadata = doc.get("metadata", {})

        items = [{
            "url": url,
            "markdown": markdown,
            "title": metadata.get("title", ""),
            "description": metadata.get("description", ""),
        }]

        return CollectResult(
            source=self.id,
            kind="page_content",
            items=items,
            cost={"units": 1, "currency": "credit"},
            metadata={"url": url},
        )

    def validate_config(self, config: dict) -> list[str]:
        errors = []
        if not config.get("api_key"):
            errors.append("api_key is required")
        return errors


def create_plugin() -> SourcePlugin:
    return FirecrawlSourcePlugin()
