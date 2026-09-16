"""GA4 Data API 适配器（第一方事件/转化/留存数据）

文档: https://developers.google.com/analytics/devguides/reporting/data/v1/
配额: 200,000 tokens/property/天、40,000/时、并发 10
注意: 在请求中加 returnPropertyQuota:true 可在响应中查配额余额
使用纯 httpx 调用 GA4 Data API v1beta REST 接口
"""

from datetime import date, timedelta

import httpx

from insflow.collectors.base import CollectContext, CollectResult, SourcePlugin

API_BASE = "https://analyticsdata.googleapis.com/v1beta"


class GA4SourcePlugin(SourcePlugin):
    """GA4 数据源插件

    采集会话/用户/转化/留存指标（sessionDefaultChannelGroup、eventName 维度）
    """

    @property
    def id(self) -> str:
        return "ga4"

    @property
    def name(self) -> str:
        return "GA4 Data API"

    @property
    def capabilities(self) -> dict:
        return {
            "metrics": ["ga4_sessions", "ga4_conversions", "ga4_users", "ga4_retention"],
            "engines": ["ga4"],
            "latency": "standard",
            "cost_hint": "Free（200k tokens/天/property）",
        }

    async def collect(self, ctx: CollectContext) -> CollectResult:
        """采集 GA4 报告数据"""
        token = ctx.config.get("access_token", "")
        property_id = ctx.config.get("property_id", "")
        if not token or not property_id:
            raise ValueError("ga4: access_token 与 property_id 必填")

        days_back = ctx.config.get("days_back", 7)
        end = date.today()
        start = end - timedelta(days=days_back)

        # 可配置的指标与维度
        metrics = ctx.config.get("metrics", ["sessions", "conversions", "totalUsers", "screenPageViews"])
        dimensions = ctx.config.get("dimensions", ["date"])

        body = {
            "dateRanges": [{"startDate": start.isoformat(), "endDate": end.isoformat()}],
            "metrics": [{"name": m} for m in metrics],
            "dimensions": [{"name": d} for d in dimensions],
            "returnPropertyQuota": True,  # 配额感知
            "limit": ctx.config.get("row_limit", 100),
        }

        headers = {"Authorization": f"Bearer {token}"}

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{API_BASE}/properties/{property_id}:runReport",
                json=body,
                headers=headers,
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()

        # 配额感知：读取剩余配额并记录
        quota = data.get("propertyQuota", {})
        tokens_left = quota.get("tokensPerDay", {}).get("remaining", 0)

        # 解析行列
        dimension_headers = [h["name"] for h in data.get("dimensionHeaders", [])]
        metric_headers = [h["name"] for h in data.get("metricHeaders", [])]

        items = []
        for row in data.get("rows", []):
            dim_values = row.get("dimensionValues", [])
            met_values = row.get("metricValues", [])
            item = {
                "dimensions": {
                    dimension_headers[i]: dv.get("value", "")
                    for i, dv in enumerate(dim_values)
                },
                "metrics": {
                    metric_headers[i]: float(mv.get("value", 0))
                    for i, mv in enumerate(met_values)
                },
            }
            items.append(item)

        return CollectResult(
            source=self.id,
            kind="ga4_report",
            items=items,
            cost={"units": 0, "currency": "USD"},
            metadata={
                "property_id": property_id,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "tokens_remaining_day": tokens_left,
            },
        )

    async def check_health(self, config: dict) -> bool:
        token = config.get("access_token", "")
        if not token:
            return False
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{API_BASE}/properties/{config.get('property_id', '0')}:metadata",
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
        if not config.get("property_id"):
            errors.append("property_id is required")
        return errors


def create_plugin() -> SourcePlugin:
    return GA4SourcePlugin()
