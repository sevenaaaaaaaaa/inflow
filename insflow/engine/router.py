"""Insight Flow 模型路由

模型路由决定哪些模型该跑、跑的顺序
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..core.entities import Metric


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
    """模型路由（含自进化权重：高权重模型优先、低权重可被过滤）"""

    def __init__(self):
        self._models: dict[str, InsightModel] = {}
        self._weights: dict[str, float] = {}
        self._weight_min = 0.0

    def set_weights(self, weights: dict | None, *, min_weight: float = 0.0) -> None:
        """设置模型权重（来自工作区设置 model_weights，由 evolution.apply 写入）"""
        self._weights = {str(k): float(v) for k, v in (weights or {}).items()}
        self._weight_min = float(min_weight or 0.0)

    def get_weight(self, model_id: str) -> float:
        return self._weights.get(str(model_id), 1.0)

    async def load_weights(self, workspace_id: str) -> dict:
        """从工作区设置加载权重（在跑模型前调用）"""
        from ..core.store import get_store
        ws = await (await get_store()).get_workspace(workspace_id)
        weights = dict((ws.settings_json or {}).get("model_weights") or {}) if ws else {}
        self.set_weights(weights)
        return weights

    def register(self, model: InsightModel) -> None:
        """注册模型"""
        self._models[model.id] = model

    def get(self, model_id: str) -> InsightModel | None:
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
                "weight": self.get_weight(m.id),
            }
            for m in self._models.values()
        ]

    def get_models_for_metrics(self, available_metrics: list[str]) -> list[InsightModel]:
        """根据可用指标获取可运行的模型（按自进化权重降序；低于下限的过滤）"""
        result = []
        for model in self._models.values():
            if all(m in available_metrics for m in model.required_metrics):
                if self._weight_min and self.get_weight(model.id) < self._weight_min:
                    continue
                result.append(model)
        result.sort(key=lambda m: -self.get_weight(m.id))
        return result


# 全局实例
_model_router: ModelRouter | None = None

_BUILTIN_MODELS_LOADED = False


def _load_builtin_models(router: ModelRouter) -> None:
    """加载内置模型库 v1（8 个）"""
    from .models.aarrr import AARRRModel
    from .models.competitor_momentum import CompetitorMomentumModel
    from .models.growth_models import (
        JourneyGapModel,
        KeywordOpportunityModel,
        NPSModel,
        PricingWatchModel,
        RetentionHealthModel,
    )
    from .models.ltv_cac import LTCACModel

    router.register(AARRRModel())
    router.register(CompetitorMomentumModel())
    router.register(LTCACModel())
    router.register(KeywordOpportunityModel())
    router.register(RetentionHealthModel())
    router.register(PricingWatchModel())
    router.register(NPSModel())
    router.register(JourneyGapModel())


def get_model_router() -> ModelRouter:
    """获取全局模型路由"""
    global _model_router, _BUILTIN_MODELS_LOADED
    if _model_router is None:
        _model_router = ModelRouter()
    if not _BUILTIN_MODELS_LOADED:
        _load_builtin_models(_model_router)
        _BUILTIN_MODELS_LOADED = True
    return _model_router


SEVERITY_SCORE = {"critical": 4.0, "high": 3.0, "medium": 2.0, "low": 1.0, "info": 0.5}


def rank_insights(insights: list, weights: dict | None = None) -> list:
    """按「严重度 × 模型权重」排序（自进化权重影响处理优先级）

    weights: {model_id: weight}；洞察的 models_json 里记录产出模型。
    """
    weights = weights or {}

    def _score(ins) -> float:
        sev = getattr(ins.severity, "value", str(ins.severity))
        base = SEVERITY_SCORE.get(str(sev), 1.0)
        models = getattr(ins, "models_json", None) or []
        w = max((float(weights.get(str(m), 1.0)) for m in models), default=1.0)
        return base * w

    return sorted(insights, key=_score, reverse=True)
