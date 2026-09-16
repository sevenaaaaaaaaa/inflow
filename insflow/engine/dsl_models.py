"""Insight Flow 自定义规则模型 DSL（IM-3，无代码配置）

面向增长运营的 JSON DSL：
{
  "id": "high-cac-warning",
  "name": "CAC 超标告警",
  "metric": "cac",                    // 关注的指标
  "condition": {"op": ">=", "value": 500},
  "window": "latest",                 // latest | any | trend_down_pct
  "severity": "high",
  "confidence": 0.8,
  "insight_type": "cac_overrun",
  "title_template": "CAC 达到 {value:.0f}，超过阈值 {threshold}",
  "summary_template": "指标 {metric} 当前为 {value}，触发条件 {op} {threshold}。",
  "actions": [{"action_type": "investigate", "description": "分渠道核算获客成本"}],
  "stage_tags": ["S1"]
}

无代码：运营配置 JSON 即可产出模型，evaluate 时按条件匹配指标生成洞察。
"""

import json
import operator as pyop
from typing import Any

from .router import InsightModel, ModelContext, get_model_router

# DSL 支持的比较运算符
OPS = {
    ">=": pyop.ge,
    ">": pyop.gt,
    "<=": pyop.le,
    "<": pyop.lt,
    "==": pyop.eq,
    "!=": pyop.ne,
}

VALID_SEVERITIES = {"critical", "high", "medium", "low", "info"}


class DSLValidationError(Exception):
    """DSL 校验失败"""
    pass


def validate_dsl(spec: dict) -> list[str]:
    """静态校验 DSL（IM-3 无代码的安全边界）"""
    errors = []
    if not spec.get("id"):
        errors.append("缺少 id")
    if not spec.get("metric"):
        errors.append("缺少 metric（关注指标名）")
    cond = spec.get("condition", {})
    if not isinstance(cond, dict):
        errors.append("condition 必须是对象")
    else:
        if cond.get("op") not in OPS:
            errors.append(f"condition.op 必须是 {list(OPS)} 之一")
        if "value" not in cond or isinstance(cond.get("value"), str):
            errors.append("condition.value 必须是数字")
    if spec.get("severity") and spec["severity"] not in VALID_SEVERITIES:
        errors.append(f"severity 必须是 {sorted(VALID_SEVERITIES)} 之一")
    for field in ("title_template", "summary_template"):
        t = spec.get(field, "")
        if "{" in t and "}" in t and ("password" in t.lower() or "secret" in t.lower()):
            errors.append(f"{field} 不得包含疑似密钥文本")
    return errors


class RuleModel(InsightModel):
    """DSL 定义的规则模型（运行时动态生成）"""

    def __init__(self, spec: dict):
        errors = validate_dsl(spec)
        if errors:
            raise DSLValidationError(f"DSL 校验失败: {errors}")
        self._spec = spec

    @property
    def id(self) -> str:
        return self._spec["id"]

    @property
    def name(self) -> str:
        return self._spec.get("name") or self._spec["id"]

    @property
    def description(self) -> str:
        return f"自定义规则模型（DSL）：{self._spec['metric']} {self._spec['condition']['op']} {self._spec['condition']['value']}"

    @property
    def required_metrics(self) -> list[str]:
        return [self._spec["metric"]]

    @property
    def spec(self) -> dict:
        return self._spec

    async def evaluate(self, ctx: ModelContext) -> list[dict]:
        spec = self._spec
        cond = spec["condition"]
        check = OPS[cond["op"]]
        threshold = cond["value"]
        metric_name = spec["metric"]
        window = spec.get("window", "latest")

        insights: list[dict] = []

        if window == "trend_down_pct":
            trend_threshold = spec.get("trend_pct", 0.2)
            series = sorted([m for m in ctx.metrics if m.metric == metric_name], key=lambda m: m.ts)
            if len(series) >= 2:
                prev, latest = series[-2].value, series[-1].value
                if prev > 0:
                    pct = (latest - prev) / prev
                    if pct <= -trend_threshold:
                        insights.append(self._draft(
                            value=latest, threshold=trend_threshold,
                            extra={"window": window, "change_pct": round(pct, 4)},
                        ))
        else:
            candidates = [m for m in ctx.metrics if m.metric == metric_name]
            if window == "any":
                hits = [m for m in candidates if check(m.value, threshold)]
            else:  # latest
                hits = []
                if candidates:
                    latest = max(candidates, key=lambda m: m.ts)
                    if check(latest.value, threshold):
                        hits = [latest]

            for m in hits:
                insights.append(self._draft(value=m.value, threshold=threshold))

        return insights

    def _draft(self, value: float, threshold: float, extra: dict | None = None) -> dict:
        spec = self._spec
        fmt = {
            "value": value, "threshold": threshold, "metric": spec["metric"],
            **(extra or {}),
        }
        title = spec.get("title_template", f"{spec['metric']} 触发阈值")
        summary = spec.get("summary_template", "指标触发自定义规则。")
        try:
            title = title.format(**fmt)
            summary = summary.format(**fmt)
        except (KeyError, IndexError, ValueError):
            pass

        return {
            "type": spec.get("insight_type", "custom_rule"),
            "title": title,
            "summary": summary,
            "severity": spec.get("severity", "medium"),
            "confidence": spec.get("confidence", 0.7),
            "evidence_json": [{
                "type": "dsl_rule",
                "rule_id": spec["id"],
                "metric": spec["metric"],
                "condition": spec["condition"],
                "observed": value,
                **(extra or {}),
            }],
            "models_json": [spec["id"]],
            "actions_json": spec.get("actions", []),
            "stage_tags_json": spec.get("stage_tags", []),
        }


class DSLRegistry:
    """自定义模型注册表（多 workspace 隔离）"""

    def __init__(self):
        self._rules: dict[tuple[str, str], RuleModel] = {}

    def register(self, workspace_id: str, spec: dict) -> RuleModel:
        model = RuleModel(spec)
        self._rules[(workspace_id, model.id)] = model
        return model

    def unregister(self, workspace_id: str, rule_id: str) -> bool:
        return self._rules.pop((workspace_id, rule_id), None) is not None

    def get(self, workspace_id: str, rule_id: str) -> RuleModel | None:
        return self._rules.get((workspace_id, rule_id))

    def list_for(self, workspace_id: str) -> list[dict]:
        return [m.spec for (ws, _), m in self._rules.items() if ws == workspace_id]


# 全局实例
_dsl_registry = DSLRegistry()


def get_dsl_registry() -> DSLRegistry:
    return _dsl_registry
