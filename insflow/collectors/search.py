"""Insight Flow 搜索源抽象层

SearchProvider 统一接口 - 解耦搜索供应商（ADR-3）
Bing 2025-08 退役、Google CSE 2027-01 停服的教训：供应商不可绑定
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum


class SearchEngine(str, Enum):
    """搜索引擎"""
    GOOGLE = "google"
    BING = "bing"
    BRAVE = "brave"
    YANDEX = "yandex"
    BAIDU = "baidu"


@dataclass
class SearchQuery:
    """搜索查询"""
    query: str
    engine: SearchEngine = SearchEngine.GOOGLE
    country: str = "us"
    language: str = "en"
    num_results: int = 10
    page: int = 1
    date_from: str | None = None  # YYYY-MM-DD
    date_to: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class SearchResult:
    """搜索结果"""
    title: str
    url: str
    snippet: str
    position: int
    domain: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class SearchResponse:
    """搜索响应"""
    query: str
    engine: str
    results: list[SearchResult] = field(default_factory=list)
    total_results: int = 0
    search_time_ms: int = 0
    cost: dict = field(default_factory=dict)  # {"units": 0.006, "currency": "USD"}
    captured_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    raw: dict = field(default_factory=dict)  # 原始响应（调试用）

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "engine": self.engine,
            "results": [
                {
                    "title": r.title,
                    "url": r.url,
                    "snippet": r.snippet,
                    "position": r.position,
                    "domain": r.domain,
                }
                for r in self.results
            ],
            "total_results": self.total_results,
            "cost": self.cost,
            "captured_at": self.captured_at.isoformat(),
        }


class SearchProvider(ABC):
    """搜索源提供者抽象基类

    所有搜索适配器必须实现此接口，确保：
    1. 供应商可替换（不绑定单一 API）
    2. 统一的查询/响应格式
    3. 配额追踪（cost 字段）
    """

    @property
    @abstractmethod
    def id(self) -> str:
        """提供者ID"""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """提供者名称"""
        ...

    @property
    def supported_engines(self) -> list[SearchEngine]:
        """支持的搜索引擎"""
        return [SearchEngine.GOOGLE]

    @property
    def cost_hint(self) -> str:
        """成本提示（供 UI 显示）"""
        return ""

    @abstractmethod
    async def search(self, query: SearchQuery, config: dict) -> SearchResponse:
        """执行搜索

        Args:
            query: 搜索查询
            config: 提供者配置（API key 等）

        Returns:
            SearchResponse
        """
        ...

    async def check_health(self, config: dict) -> bool:
        """健康检查"""
        return True

    def validate_config(self, config: dict) -> list[str]:
        """校验配置"""
        return []
