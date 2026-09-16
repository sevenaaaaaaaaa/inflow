"""DataForSEO Ads Transparency 适配器（CI-4 广告素材情报）

文档: https://docs.dataforseo.com/v3/advertising/serp/advertisers/
用途：抓取竞品在 Google Ads 的投放素材（标题/描述/落地页/首次见刊时间）
     → 投放力度变化曲线 → 广告策略情报洞察
"""

import base64

import httpx

from insflow.collectors.base import CollectContext, CollectResult, SourcePlugin

API_URL = "https://api.dataforseo.com/v3/dataforseo_labs/google/advertisers/domains_by_search_ad_boxes/live"


class DataForSEOAdsPlugin(SourcePlugin):
    """DataForSEO Ads Transparency 数据源插件"""

    @property
    def id(self) -> str:
        return "dataforseo-ads"

    @property
    def name(self) -> str:
        return "DataForSEO Ads Transparency"

    @property
    def capabilities(self) -> dict:
        return {
            "metrics": ["ad_creatives", "ad_spend_signal"],
            "engines": ["google"],
            "latency": "standard",
            "cost_hint": "按量计费",
        }

    async def collect(self, ctx: CollectContext) -> CollectResult:
        """抓取竞品的 Google 广告素材"""
        login = ctx.config.get("login", "")
        password = ctx.config.get("password", "")
        if not login or not password:
            raise ValueError("dataforseo-ads: login/password 必填")

        domain = ctx.config.get("domain", "")
        if not domain:
            raise ValueError("dataforseo-ads: domain 必填")

        location = ctx.config.get("location_code", 2840)
        language = ctx.config.get("language_code", "en")
        limit = ctx.config.get("limit", 100)

        headers = {
            "Authorization": f"Basic {base64.b64encode(f'{login}:{password}'.encode()).decode()}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.dataforseo.com/v3/dataforseo_labs/google/advertisers/domains_by_search_advertisers/live",
                json=[{
                    "domain": domain,
                    "location_code": location,
                    "language_code": language,
                    "limit": limit,
                }],
                headers=headers,
                timeout=60.0,
            )
            resp.raise_for_status()
            data = resp.json()

        cost = data.get("cost", 0.0)
        items = []
        tasks = data.get("tasks", [])
        if tasks and tasks[0].get("result"):
            for item in tasks[0]["result"][0].get("items", []):
                ad = item.get("ad", {}) if isinstance(item, dict) else {}
                items.append({
                    "advertiser_domain": domain,
                    "creative_title": ad.get("title", ""),
                    "creative_body": ad.get("text", ""),
                    "landing_url": ad.get("url", "") or item.get("url", ""),
                    "first_seen": ad.get("first_seen", ""),
                    "last_seen": ad.get("last_seen", ""),
                })

        return CollectResult(
            source=self.id,
            kind="ad_creatives",
            items=items,
            cost={"units": cost, "currency": "USD"},
            metadata={"domain": domain},
        )

    def validate_config(self, config: dict) -> list[str]:
        errors = []
        if not config.get("login"):
            errors.append("login is required")
        if not config.get("password"):
            errors.append("password is required")
        return errors


def create_plugin() -> SourcePlugin:
    return DataForSEOAdsPlugin()
