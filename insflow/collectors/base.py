"""Insight Flow 采集器基类"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


@dataclass
class CollectContext:
    """采集上下文"""
    workspace_id: str
    monitor_id: str
    config: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)


@dataclass
class CollectResult:
    """采集结果信封"""
    source: str
    kind: str
    captured_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    items: list[dict] = field(default_factory=list)
    cost: dict = field(default_factory=dict)  # {"units": 0.06, "currency": "USD"}
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "source": self.source,
            "kind": self.kind,
            "captured_at": self.captured_at.isoformat(),
            "items": self.items,
            "cost": self.cost,
            "metadata": self.metadata,
        }


class SourcePlugin(ABC):
    """数据源插件基类"""

    @property
    @abstractmethod
    def id(self) -> str:
        """插件ID"""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """插件名称"""
        ...

    @property
    def capabilities(self) -> dict:
        """能力声明（供模型路由匹配）"""
        return {
            "metrics": [],
            "engines": [],
            "latency": "standard",
            "cost_hint": "",
        }

    @abstractmethod
    async def collect(self, ctx: CollectContext) -> CollectResult:
        """执行采集"""
        ...

    async def check_health(self, config: dict) -> bool:
        """健康检查（测试连接/凭据）"""
        return True

    def validate_config(self, config: dict) -> list[str]:
        """校验配置，返回错误列表"""
        return []
