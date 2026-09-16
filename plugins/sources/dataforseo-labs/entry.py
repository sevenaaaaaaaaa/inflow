"""DataForSEO Labs 适配器（SEO 竞争情报：关键词缺口 / 域名排名）

文档: https://docs.dataforseo.com/v3/dataforseo_labs/
用于 CI-3：
- domain_intersection：我方与竞品的关键词交集（重叠）
- domain_who_ranks：竞品有排名而我方没有（关键词缺口）
- ranked_keywords：域名下全部有排名的关键词（排名追踪）

成本控制：每调用都返回 cost，进配额账本。
"""

import base64

import httpx

from insflow.collectors.base import CollectContext, CollectResult, SourcePlugin

API_BASE = "https://api.dataforseo.com/v3/dataforseo_labs/google"


def _auth(login: str, password: str) -> str:
    return base64.b64encode(f"{login}:{password}".encode()).decode()


class DataForSEOLabsPlugin(SourcePlugin):
    """DataForSEO Labs 数据源插件"""

    @property
    def id(self) -> str:
        return "dataforseo-labs"

    @property
    def name(self) -> str:
        return "DataForSEO Labs"

    @property
    def capabilities(self) -> dict:
        return {
            "metrics": ["keyword_gap", "domain_ranks", "domain_intersection"],
            "engines": ["google"],
            "latency": "standard",
            "cost_hint": "按量计费（成本受控）",
        }

    def _headers(self, config: dict) -> dict:
        login = config.get("login", "")
        password = config.get("password", "")
        if not login or not password:
            raise ValueError("dataforseo-labs: login/password 必填")
        return {
            "Authorization": f"Basic {base64.b64encode(f'{login}:{password}'.encode()).decode()}",
            "Content-Type": "application/json",
        }

    async def collect(self, ctx: CollectContext) -> CollectResult:
        """按任务类型采集（kind: keyword_gap | domain_ranks）"""
        headers = self._headers(ctx.config)
        target = ctx.config.get("target", "")  # 域名
        competitor = ctx.config.get("competitor", "")
        location = ctx.config.get("location_code", 2840)  # 默认 United States
        language = ctx.config.get("language_code", "en")
        limit = ctx.config.get("limit", 100)

        operation = ctx.config.get("operation", "domain_intersection")

        async with httpx.AsyncClient() as client:
            if operation == "domain_intersection" and competitor:
                # 关键词交集与缺口：我方 target vs 竞品 competitor
                resp = await client.post(
                    f"{API_BASE}/domain_intersection/live",
                    json=[{
                        "target1": target,
                        "target2": competitor,
                        "location_code": location,
                        "language_code": language,
                        "limit": limit,
                    }],
                    headers=headers,
                    timeout=60.0,
                )
                kind = "domain_intersection"
            else:
                # 域名全部有排名关键词
                resp = await client.post(
                    f"{API_BASE}/ranked_keywords/live",
                    json=[{
                        "target": target,
                        "location_code": location,
                        "language_code": language,
                        "limit": limit,
                    }],
                    headers=headers,
                    timeout=60.0,
                )
                kind = "domain_ranks"

            resp.raise_for_status()
            data = resp.json()

        # 计费（Labs 按结果量）
        cost = data.get("cost", 0.0)
        items = []
        tasks = data.get("tasks", [])
        if tasks and tasks[0].get("result"):
            result = tasks[0]["result"][0]
            for item in result.get("items", []):
                rank_info = item.get("rank_info", {})
                keyword_data = item.get("keyword_data", {})
                items.append({
                    "keyword": keyword_data.get("keyword", ""),
                    "position": rank_info.get("pos", 0),
                    "volume": keyword_data.get("search_volume", 0),
                    "url": rank_info.get("url", ""),
                    "competitor_domain": competitor if kind == "domain_intersection" else target,
                })

        return CollectResult(
            source=self.id,
            kind=kind,
            items=items,
            cost={"units": cost, "currency": "USD"},
            metadata={"target": target, "competitor": competitor},
        )

    def validate_config(self, config: dict) -> list[str]:
        errors = []
        if not config.get("login"):
            errors.append("login is required")
        if not config.get("password"):
            errors.append("password is required")
        return errors


def create_plugin() -> SourcePlugin:
    return DataForSEOLabsPlugin()
