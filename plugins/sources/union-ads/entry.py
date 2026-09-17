"""联盟/广告投放监测适配器（对标 Similarweb 的联盟/广告监测能力）

两类数据：自有投放（spend/clicks/conversions）与联盟分佣（revenue/orders）。
来源可选：HTTP 接口（Bearer）或离线 payload（私有化/手工导入）。
输出：campaign 级指标 + 维度 {channel, campaign, source}，供渠道箱线/桑基/地图使用。
"""

import httpx

from insflow.collectors.base import CollectContext, CollectResult, SourcePlugin


class UnionAdsProvider(SourcePlugin):
    """联盟与广告投放数据源"""

    METRIC_MAP = {
        "spend": "ad_spend", "cost": "ad_spend",
        "clicks": "ad_clicks",
        "conversions": "ad_conversions", "conv": "ad_conversions",
        "revenue": "affiliate_revenue", "commission": "affiliate_revenue",
        "orders": "affiliate_orders",
    }

    @property
    def id(self) -> str:
        return "union-ads"

    @property
    def name(self) -> str:
        return "联盟/广告投放监测"

    @property
    def capabilities(self) -> dict:
        return {
            "metrics": ["ad_spend", "ad_clicks", "ad_conversions",
                        "affiliate_revenue", "affiliate_orders"],
            "engines": ["union-ads"],
            "latency": "standard",
            "cost_hint": "自建接口/联盟 API；无第三方订阅费",
        }

    async def _fetch(self, endpoint: str, api_key: str = "",
                     timeout: float = 30.0) -> list[dict]:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        async with httpx.AsyncClient() as client:
            resp = await client.get(endpoint, headers=headers, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
        if isinstance(data, dict):
            for key in ("data", "rows", "items", "results"):
                if isinstance(data.get(key), list):
                    return data[key]
            return [data]
        return data if isinstance(data, list) else []

    def _rows(self, raw: list[dict]) -> list[dict]:
        """原始行 → 指标行（未知指标跳过，不做猜测）"""
        out = []
        for r in raw:
            if not isinstance(r, dict):
                continue
            campaign = str(r.get("campaign") or r.get("campaign_name") or "未命名")[:60]
            channel = str(r.get("channel") or r.get("network") or
                          r.get("source") or "union")[:40]
            source = str(r.get("source") or r.get("partner") or channel)[:40]
            ts = str(r.get("ts") or r.get("date") or "") or None
            for raw_key, metric in self.METRIC_MAP.items():
                if raw_key not in r or r[raw_key] in (None, ""):
                    continue
                try:
                    value = float(r[raw_key])
                except (TypeError, ValueError):
                    continue
                row = {
                    "entity_type": "campaign", "entity_id": campaign,
                    "metric": metric, "value": value,
                    "dim": {"channel": channel, "campaign": campaign,
                            "source": source},
                }
                if ts:
                    row["ts"] = ts
                out.append(row)
        return out

    async def collect(self, ctx: CollectContext) -> CollectResult:
        rows: list[dict] = []
        payload = ctx.config.get("payload")
        if isinstance(payload, list) and payload:
            rows = self._rows(payload)
        else:
            endpoint = ctx.config.get("endpoint") or ""
            if not endpoint:
                return CollectResult(source=self.id, kind="ads_union", items=[],
                                     metadata={"skipped": "缺少 endpoint / payload"})
            raw = await self._fetch(endpoint, ctx.config.get("api_key", ""))
            rows = self._rows(raw)
        return CollectResult(source=self.id, kind="ads_union", items=rows,
                             cost={"units": 1, "currency": "USD"},
                             metadata={"campaigns": len({r["entity_id"] for r in rows})})

    async def check_health(self, config: dict) -> bool:
        if config.get("payload"):
            return True
        if not config.get("endpoint"):
            return False
        try:
            await self._fetch(config["endpoint"], config.get("api_key", ""), timeout=8)
            return True
        except Exception:
            return False


def create_plugin() -> SourcePlugin:
    """注册表工厂（插件宿主约定）"""
    return UnionAdsProvider()
