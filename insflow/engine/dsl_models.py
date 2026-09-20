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

import operator as pyop

from .router import InsightModel, ModelContext

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


# ========== 持久化（批次 H 修复）==========
#
# 注册表本来只在内存里：API 注册、模板包 apply、结构提案生效的规则，**进程一重启就没了**，
# 而且没有任何报错——用户只会发现"配的模型莫名其妙不跑了"。规则本身是配置（不是数据），
# 随工作区 settings_json 走最合适：多实例共享、跟着备份走、导出模板包时能带上。

SETTINGS_KEY = "dsl_rules"


async def _load_settings(workspace_id: str):
    from ..core.store import get_store
    store = await get_store()
    ws = await store.get_workspace(workspace_id)
    if not ws:
        raise DSLValidationError("工作区不存在")
    return store, ws, dict(ws.settings_json or {})


async def save_rule(workspace_id: str, spec: dict) -> dict:
    """注册 + 落库（同 id 覆盖）。校验失败抛 DSLValidationError，不落任何东西。"""
    model = get_dsl_registry().register(workspace_id, spec)   # 先过校验
    store, ws, settings = await _load_settings(workspace_id)
    rules = [r for r in (settings.get(SETTINGS_KEY) or [])
             if isinstance(r, dict) and r.get("id") != model.id]
    rules.append(dict(spec))
    settings[SETTINGS_KEY] = rules
    ws.settings_json = settings
    await store.update_workspace(ws)
    return dict(spec)


async def remove_rule(workspace_id: str, rule_id: str) -> bool:
    """摘除 + 落库删除（内存或库里任一存在即算删掉）"""
    in_memory = get_dsl_registry().unregister(workspace_id, rule_id)
    store, ws, settings = await _load_settings(workspace_id)
    rules = settings.get(SETTINGS_KEY) or []
    kept = [r for r in rules if not (isinstance(r, dict) and r.get("id") == rule_id)]
    changed = len(kept) != len(rules)
    if changed:
        settings[SETTINGS_KEY] = kept
        ws.settings_json = settings
        await store.update_workspace(ws)
    return in_memory or changed


async def persisted_rules(workspace_id: str) -> list[dict]:
    """库里存着的规则（与内存注册表可能短暂不一致时以库为准）"""
    from ..core.store import get_store
    store = await get_store()
    ws = await store.get_workspace(workspace_id)
    settings = dict((ws.settings_json if ws else {}) or {})
    return [r for r in (settings.get(SETTINGS_KEY) or []) if isinstance(r, dict)]


async def restore_rules() -> int:
    """启动时把各工作区落库的规则装回注册表（bootstrap 调用一次）"""
    from ..core.store import get_store
    store = await get_store()
    registry = get_dsl_registry()
    restored = 0
    for ws in await store.list_workspaces():
        for spec in (dict(ws.settings_json or {}).get(SETTINGS_KEY) or []):
            if not isinstance(spec, dict):
                continue
            try:
                registry.register(ws.id, spec)
                restored += 1
            except DSLValidationError:
                import logging
                logging.getLogger("insflow.dsl").warning(
                    "跳过非法 DSL 规则: %s/%s", ws.id, spec.get("id"))
    return restored
