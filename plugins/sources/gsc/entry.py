"""Google Search Console API 适配器（TD-1 主数据源）

文档: https://developers.google.com/webmaster-tools/
配额: Search Analytics 1,200 QPM/site；数据滞后约数天
使用纯 httpx 调用（不依赖 google-api-python-client，保持轻依赖家族哲学）
"""

import httpx
from datetime import date, timedelta

from insflow.collectors.base import CollectContext, CollectResult, SourcePlugin

API_BASE = "https://searchconsole.googleapis.com/webmasters/v3"


class GSCSourcePlugin(SourcePlugin):
    """GSC 数据源插件

    采集 Search Analytics 数据：点击/曝光/CTR/排名（query/page 维度）
    """

    @property
    def id(self) -> str:
        return "gsc"

    @property
    def name(self) -> str:
        return "Google Search Console API"

    @property
    def capabilities(self) -> dict:
        return {
            "metrics": ["gsc_clicks", "gsc_impressions", "gsc_ctr", "gsc_position"],
            "engines": ["google"],
            "latency": "standard",
            "cost_hint": "Free（1200 QPM/site）",
        }

    async def collect(self, ctx: CollectContext) -> CollectResult:
        """采集 GSC Search Analytics 数据"""
        token = ctx.config.get("access_token", "")
        site_url = ctx.config.get("site_url", "")
        if not token or not site_url:
            raise ValueError("gsc: access_token 与 site_url 必填")

        # 默认拉取最近 7 天（GSC 数据滞后约 2-3 天）
        days_back = ctx.config.get("days_back", 10)
        end = date.today() - timedelta(days=3)
        start = end - timedelta(days=days_back)

        dimension = ctx.config.get("dimension", "query")  # query | page | date
        row_limit = ctx.config.get("row_limit", 100)

        body = {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "dimensions": [dimension],
            "rowLimit": row_limit,
            "dataState": "all",  # 含新鲜数据（fresh）
        }

        headers = {"Authorization": f"Bearer {token}"}

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{API_BASE}/sites/{site_url}/searchAnalytics/query",
                json=body,
                headers=headers,
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()

        items = []
        for row in data.get("rows", []):
            keys = row.get("keys", [])
            items.append({
                "dimension": dimension,
                "key": keys[0] if keys else "",
                "clicks": row.get("clicks", 0),
                "impressions": row.get("impressions", 0),
                "ctr": row.get("ctr", 0.0),
                "position": row.get("position", 0.0),
            })

        return CollectResult(
            source=self.id,
            kind="gsc_search_analytics",
            items=items,
            cost={"units": 0, "currency": "USD"},
            metadata={
                "site": site_url,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "dimension": dimension,
            },
        )

    async def check_health(self, config: dict) -> bool:
        """健康检查：列出可访问站点"""
        token = config.get("access_token", "")
        if not token:
            return False
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{API_BASE}/sites",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=10.0,
                )
                return resp.status_code == 200
        except Exception:
            return False

    def validate_config(self, config: dict) -> list[str]:
        errors = []
        if not config.get("access_token"):
            errors.append("access_token is required")
        if not config.get("site_url"):
            errors.append("site_url is required")
        return errors


def create_plugin() -> SourcePlugin:
    return GSCSourcePlugin()
