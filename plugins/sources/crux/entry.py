"""CrUX API 适配器（真实用户 Core Web Vitals）

文档: https://developer.chrome.com/docs/crux/api/
配额: 150 QPM/project，免费不可提额
数据: LCP/INP/CLS/TTFB，origin/url 级，28 天滚动窗口
注意: PSI 的 field data 将移除，直接用 CrUX API
"""

import httpx

from insflow.collectors.base import CollectContext, CollectResult, SourcePlugin

API_URL = "https://chromeuxreport.googleapis.com/v1/records:queryRecord"


class CrUXSourcePlugin(SourcePlugin):
    """CrUX 数据源插件

    采集真实用户 CWV 指标（LCP/INP/CLS/TTFB）
    """

    @property
    def id(self) -> str:
        return "crux"

    @property
    def name(self) -> str:
        return "CrUX API"

    @property
    def capabilities(self) -> dict:
        return {
            "metrics": ["crux_lcp", "crux_inp", "crux_cls", "crux_ttfb"],
            "engines": ["crux"],
            "latency": "standard",
            "cost_hint": "Free（150 QPM）",
        }

    async def collect(self, ctx: CollectContext) -> CollectResult:
        """采集 CWV 指标"""
        api_key = ctx.config.get("api_key", "")
        if not api_key:
            raise ValueError("crux: api_key 必填")

        # origin 模式（整站）或 url 模式（单页）
        origin = ctx.config.get("origin", "")
        url = ctx.config.get("url", "")
        form_factor = ctx.config.get("form_factor", "ALL")  # ALL | PHONE | DESKTOP | TABLET

        if not origin and not url:
            raise ValueError("crux: origin 或 url 必须提供一个")

        body: dict = {"formFactor": form_factor}
        if origin:
            body["origin"] = origin
        if url:
            body["url"] = url

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{API_URL}?key={api_key}",
                json=body,
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()

        items = []
        record = data.get("record", {})
        metrics = record.get("metrics", {})

        for metric_name, metric_data in metrics.items():
            percentiles = metric_data.get("percentiles", {}).get("p75", 0)
            categories = metric_data.get("histogram", [])
            # 判断是否达标（good 桶占比）
            good_fraction = 0.0
            if categories:
                good_bucket = categories[0].get("density", 0.0)
                good_fraction = good_bucket
            items.append({
                "metric": metric_name,
                "p75": percentiles,
                "good_fraction": round(good_fraction, 4),
                "category": "good" if good_fraction >= 0.75 else ("poor" if good_fraction < 0.5 else "needs_improvement"),
            })

        return CollectResult(
            source=self.id,
            kind="crux_cwv",
            items=items,
            cost={"units": 0, "currency": "USD"},
            metadata={
                "target": origin or url,
                "form_factor": form_factor,
                "collection_period": record.get("collectionPeriod", {}),
            },
        )

    async def check_health(self, config: dict) -> bool:
        api_key = config.get("api_key", "")
        if not api_key:
            return False
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{API_URL}?key={api_key}",
                    json={"origin": "https://example.com"},
                    timeout=10.0,
                )
                # 200（有数据）或 404（origin 无数据但 key 有效）
                return resp.status_code in (200, 404)
        except Exception:
            return False

    def validate_config(self, config: dict) -> list[str]:
        errors = []
        if not config.get("api_key"):
            errors.append("api_key is required")
        return errors


def create_plugin() -> SourcePlugin:
    return CrUXSourcePlugin()
