"""Reddit Official API 适配器

文档: https://www.reddit.com/dev/api/
免费 API，需要 OAuth2 认证
"""

import httpx
from datetime import datetime, timezone
from typing import Any

from insflow.collectors.base import CollectContext, CollectResult, SourcePlugin


class RedditProvider:
    """Reddit API 提供者"""

    AUTH_URL = "https://www.reddit.com/api/v1/access_token"
    API_URL = "https://oauth.reddit.com"

    def __init__(self):
        self._token: str = ""
        self._token_expires: datetime = datetime.min.replace(tzinfo=timezone.utc)

    async def _get_token(self, config: dict) -> str:
        """获取 OAuth2 访问令牌"""
        if self._token and datetime.now(timezone.utc) < self._token_expires:
            return self._token

        client_id = config.get("client_id", "")
        client_secret = config.get("client_secret", "")
        user_agent = config.get("user_agent", "InsightFlow/1.0")

        auth = httpx.BasicAuth(client_id, client_secret)
        data = {
            "grant_type": "client_credentials",
        }
        headers = {"User-Agent": user_agent}

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self.AUTH_URL,
                auth=auth,
                data=data,
                headers=headers,
                timeout=30.0,
            )
            resp.raise_for_status()
            token_data = resp.json()

        self._token = token_data["access_token"]
        # Token 有效期 24 小时，提前 1 小时刷新
        self._token_expires = datetime.now(timezone.utc).replace(
            hour=datetime.now(timezone.utc).hour + 23
        )
        return self._token

    async def search(self, query: str, subreddit: str = "", sort: str = "relevance",
                     time_filter: str = "all", limit: int = 25, config: dict = None) -> dict:
        """搜索帖子"""
        token = await self._get_token(config or {})
        user_agent = config.get("user_agent", "InsightFlow/1.0")

        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": user_agent,
        }

        # 构建搜索 URL
        if subreddit:
            url = f"{self.API_URL}/r/{subreddit}/search"
        else:
            url = f"{self.API_URL}/search"

        params = {
            "q": query,
            "sort": sort,
            "t": time_filter,
            "limit": limit,
            "type": "link",
        }

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url,
                params=params,
                headers=headers,
                timeout=30.0,
            )
            resp.raise_for_status()
            return resp.json()

    async def get_subreddit_posts(self, subreddit: str, sort: str = "hot",
                                  limit: int = 25, config: dict = None) -> dict:
        """获取 subreddit 帖子"""
        token = await self._get_token(config or {})
        user_agent = config.get("user_agent", "InsightFlow/1.0")

        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": user_agent,
        }

        url = f"{self.API_URL}/r/{subreddit}/{sort}"
        params = {"limit": limit}

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url,
                params=params,
                headers=headers,
                timeout=30.0,
            )
            resp.raise_for_status()
            return resp.json()


class RedditSourcePlugin(SourcePlugin):
    """Reddit 数据源插件"""

    def __init__(self):
        self._provider = RedditProvider()

    @property
    def id(self) -> str:
        return "reddit"

    @property
    def name(self) -> str:
        return "Reddit Official API"

    @property
    def capabilities(self) -> dict:
        return {
            "metrics": ["reddit_posts", "reddit_comments", "subreddit_mentions"],
            "engines": ["reddit"],
            "latency": "standard",
            "cost_hint": "Free (100 req/min)",
        }

    async def collect(self, ctx: CollectContext) -> CollectResult:
        """执行采集"""
        query = ctx.config.get("query", "")
        subreddit = ctx.config.get("subreddit", "")
        sort = ctx.config.get("sort", "relevance")
        time_filter = ctx.config.get("time_filter", "all")
        limit = ctx.config.get("limit", 25)

        data = await self._provider.search(
            query=query,
            subreddit=subreddit,
            sort=sort,
            time_filter=time_filter,
            limit=limit,
            config=ctx.config,
        )

        # 解析帖子列表
        items = []
        for child in data.get("data", {}).get("children", []):
            post = child.get("data", {})
            items.append({
                "id": post.get("id", ""),
                "title": post.get("title", ""),
                "url": post.get("url", ""),
                "permalink": f"https://reddit.com{post.get('permalink', '')}",
                "author": post.get("author", ""),
                "subreddit": post.get("subreddit", ""),
                "score": post.get("score", 0),
                "num_comments": post.get("num_comments", 0),
                "created_utc": post.get("created_utc", 0),
                "selftext": post.get("selftext", "")[:500],  # 截断
                "is_video": post.get("is_video", False),
            })

        return CollectResult(
            source=self.id,
            kind="reddit_posts",
            items=items,
            cost={"units": 0, "currency": "USD"},  # 免费
            metadata={"query": query, "subreddit": subreddit, "total": len(items)},
        )

    async def check_health(self, config: dict) -> bool:
        """健康检查"""
        try:
            await self._provider.search("test", limit=1, config=config)
            return True
        except Exception:
            return False

    def validate_config(self, config: dict) -> list[str]:
        errors = []
        if not config.get("client_id"):
            errors.append("client_id is required")
        if not config.get("client_secret"):
            errors.append("client_secret is required")
        return errors


def create_plugin() -> SourcePlugin:
    """插件工厂函数"""
    return RedditSourcePlugin()
