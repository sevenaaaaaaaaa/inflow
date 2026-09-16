"""Insight Flow 模型路由

模型路由决定哪些模型该跑、跑的顺序
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from ..core.entities import Insight, Metric


@dataclass
class ModelContext:
    """模型执行上下文"""
    workspace_id: str
    metrics: list[Metric] = field(default_factory=list)
    config: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)


class InsightModel(ABC):
    """洞察模型基类"""

    @property
    @abstractmethod
    def id(self) -> str:
        """模型ID"""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """模型名称"""
        ...

    @property
    def description(self) -> str:
        """模型描述"""
        return ""

    @property
    def required_metrics(self) -> list[str]:
        """需要的指标类型"""
        return []

    @abstractmethod
    async def evaluate(self, ctx: ModelContext) -> list[dict]:
        """评估并产出洞察草稿

        Args:
            ctx: 模型上下文

        Returns:
            洞察草稿列表（每个 dict 即一条 Insight 草稿）
        """
        ...


class ModelRouter:
    """模型路由"""

    def __init__(self):
        self._models: dict[str, InsightModel] = {}

    def register(self, model: InsightModel) -> None:
        """注册模型"""
        self._models[model.id] = model

    def get(self, model_id: str) -> Optional[InsightModel]:
        """获取模型"""
        return self._models.get(model_id)

    def list_models(self) -> list[dict]:
        """列出所有模型"""
        return [
            {
                "id": m.id,
                "name": m.name,
                "description": m.description,
                "required_metrics": m.required_metrics,
            }
            for m in self._models.values()
        ]

    def get_models_for_metrics(self, available_metrics: list[str]) -> list[InsightModel]:
        """根据可用指标获取可运行的模型"""
        result = []
        for model in self._models.values():
            # 检查模型所需指标是否都可用
            if all(m in available_metrics for m in model.required_metrics):
                result.append(model)
        return result


# 全局实例
_model_router: Optional[ModelRouter] = None

_BUILTIN_MODELS_LOADED = False


def _load_builtin_models(router: ModelRouter) -> None:
    """加载内置模型"""
    from .models.aarrr import AARRRModel
    from .models.competitor_momentum import CompetitorMomentumModel

    router.register(AARRRModel())
    router.register(CompetitorMomentumModel())


def get_model_router() -> ModelRouter:
    """获取全局模型路由"""
    global _model_router, _BUILTIN_MODELS_LOADED
    if _model_router is None:
        _model_router = ModelRouter()
    if not _BUILTIN_MODELS_LOADED:
        _load_builtin_models(_model_router)
        _BUILTIN_MODELS_LOADED = True
    return _model_router
