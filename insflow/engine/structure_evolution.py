"""结构进化（批次 H）：自进化从"调参数"迈到"改结构"

此前进化的对象只有**参数**（告警阈值、模型权重）。这里让系统能提出**结构**：
- `structure_dsl`    —— 从"验证有效"的配方 + 指标分布，起草一条自定义规则模型
- `structure_monitor`—— 从反复出现却没人盯的洞察类型，起草一个监控任务
- `structure_board`  —— 从真实浏览/导出行为，起草一个"常用面板"看板

护栏与参数进化同源（docs/13 §3）：
1. **只提案**：生成即 `proposed`，必须过评测门 + 人工 apply；LLM 不参与生效路径
2. **有依据才提**：配方样本 ≥3 且有效率 ≥50%；指标点数 ≥10；浏览次数 ≥阈值
3. **可回滚**：apply 时把创建出来的 id 记进 `gate_json.applied_ref`，rollback 原样撤销
4. **冷却期 + 去重**：同目标 72h 内不重复提案；已存在的规则/监控/看板不再提

不做的事：不改安全/合规/频控配置，不动已有的规则与看板（只新增，冲突即跳过）。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

MIN_PLAYBOOK_SAMPLES = 3
MIN_EFFECTIVE_RATE = 0.5
MIN_METRIC_POINTS = 10
MIN_INSIGHTS_FOR_MONITOR = 3
MIN_VIEWS_FOR_BOARD = 3
DEFAULT_MONITOR_CRON = "0 */6 * * *"

# 洞察类型关键词 → 监控类型（没命中就不提监控，宁可不提也不乱建）
TYPE_TO_MONITOR = (
    ("competitor", "site_change"),
    ("pricing", "site_change"),
    ("keyword", "keyword"),
    ("seo", "keyword"),
    ("brand", "brand_mention"),
    ("sentiment", "brand_mention"),
    ("review", "brand_mention"),
    ("topic", "topic"),
    ("journey", "journey"),
    ("funnel", "journey"),
    ("retention", "journey"),
)
# 证据里可能出现的目标字段（按优先级）
TARGET_FIELDS = ("url", "site", "domain", "page", "competitor", "brand", "keyword")
# 指标名关键词 → 越低越糟（用下尾 p05 作为触发阈值）
LOWER_IS_WORSE = ("retention", "conversion", "ctr", "session", "signup", "revenue",
                  "active", "order", "click", "roi")


def _slug(text: str) -> str:
    s = re.sub(r"[^\w-]+", "-", str(text or "")).strip("-").lower()
    return s[:32] or "auto"


def _quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    idx = min(len(ordered) - 1, max(0, int(q * len(ordered))))
    return float(ordered[idx])


def _monitor_kind_for(insight_type: str) -> str:
    low = (insight_type or "").lower()
    for token, kind in TYPE_TO_MONITOR:
        if token in low:
            return kind
    return ""


def _target_from_evidence(evidence) -> dict:
    """从证据里挑一个可监控的目标（找不到就返回空 → 不提案）"""
    for ev in (evidence or []):
        if not isinstance(ev, dict):
            continue
        for field in TARGET_FIELDS:
            value = ev.get(field)
            if isinstance(value, str) and value.strip():
                return {field: value.strip()[:200]}
    return {}


# ================= 提案：DSL 规则 =================

async def propose_dsl_rules(workspace_id: str) -> list[dict]:
    """把"验证有效的配方 + 指标分布"变成规则草案（只提案）"""
    from ..core.store import get_store
    from .dsl_models import persisted_rules, validate_dsl
    from .evolution import _in_cooldown
    from .playbooks import mine

    store = await get_store()
    runs = await store.list_evolution_runs(workspace_id, limit=200)
    existing = {r.get("id") for r in await persisted_rules(workspace_id)}
    proposals = []
    for pb in await mine(workspace_id):
        metrics = [m for m in (pb.get("metrics") or []) if m]
        if not metrics or pb.get("samples", 0) < MIN_PLAYBOOK_SAMPLES:
            continue
        if pb.get("effective_rate", 0) < MIN_EFFECTIVE_RATE:
            continue
        insight_type = str(pb.get("insight_type") or "unknown")
        metric = metrics[0]
        series = await store.metric_series(workspace_id, metric, days=90)
        if len(series) < MIN_METRIC_POINTS:
            continue
        values = [float(s["value"] or 0) for s in series]
        lower_worse = any(tok in metric.lower() for tok in LOWER_IS_WORSE)
        op, threshold, tail = ((("<="), _quantile(values, 0.05), "p05")
                               if lower_worse
                               else ((">="), _quantile(values, 0.95), "p95"))
        rule_id = f"auto-{_slug(insight_type)}-{_slug(metric)}"
        if rule_id in existing or _in_cooldown(runs, "structure_dsl", rule_id):
            continue
        spec = {
            "id": rule_id,
            "name": f"{metric} {op} {round(threshold, 4)}（结构提案）",
            "metric": metric,
            "condition": {"op": op, "value": round(threshold, 6)},
            "window": "latest",
            "severity": "medium",
            "confidence": round(min(0.9, 0.5 + pb.get("effective_rate", 0) / 2), 2),
            "insight_type": insight_type,
            "title_template": f"{metric} 触及 {tail} 分位（{{value}}）",
            "summary_template": (f"指标 {{metric}} 当前 {{value}}，触发 {{op}} {{threshold}}；"
                                 f"该场景历史上派发 {pb.get('action_type', '')} 的有效率为 "
                                 f"{pb.get('effective_rate', 0):.0%}。"),
            "actions": [{"action_type": pb.get("action_type") or "investigate",
                         "description": "按配方处置，并进入 14 天验证"}],
        }
        errors = validate_dsl(spec)
        if errors:
            continue
        run = await store.create_evolution_run(
            workspace_id, "structure_dsl", target=rule_id,
            before={}, after={"spec": spec},
            rationale={
                "source": "playbook_mining",
                "playbook": pb.get("id"),
                "samples": pb.get("samples"),
                "effective_rate": pb.get("effective_rate"),
                "metric": metric, "tail": tail,
                "points": len(values),
                "basis": (f"配方 {pb.get('id')} 样本 {pb.get('samples')} 条、有效率 "
                          f"{pb.get('effective_rate', 0):.0%}；{metric} 近 90 天 "
                          f"{len(values)} 点，{tail} = {round(threshold, 4)}"),
            })
        proposals.append(run)
    return proposals


# ================= 提案：监控任务 =================

async def propose_monitors(workspace_id: str) -> list[dict]:
    """反复出现、却没有任何监控在盯的洞察类型 → 起草监控（只提案）"""
    from ..core.store import get_store
    from .evolution import _in_cooldown

    store = await get_store()
    runs = await store.list_evolution_runs(workspace_id, limit=200)
    monitors = await store.list_monitors_full(workspace_id)
    covered = {str(m.get("kind")) for m in monitors}
    insights = await store.list_insights(workspace_id, limit=300)

    grouped: dict[str, list] = {}
    for ins in insights:
        grouped.setdefault(ins.type, []).append(ins)

    proposals = []
    for insight_type, items in sorted(grouped.items(),
                                      key=lambda kv: -len(kv[1])):
        if len(items) < MIN_INSIGHTS_FOR_MONITOR:
            continue
        kind = _monitor_kind_for(insight_type)
        if not kind or kind in covered:
            continue
        target: dict = {}
        for ins in items:
            target = _target_from_evidence(ins.evidence_json)
            if target:
                break
        if not target:
            continue                      # 证据里没有可监控目标：不编一个出来
        key = f"{kind}:{_slug(next(iter(target.values())))}"
        if _in_cooldown(runs, "structure_monitor", key):
            continue
        run = await store.create_evolution_run(
            workspace_id, "structure_monitor", target=key,
            before={},
            after={"kind": kind, "target": target,
                   "schedule_cron": DEFAULT_MONITOR_CRON},
            rationale={
                "source": "insight_gap",
                "insight_type": insight_type,
                "insight_count": len(items),
                "basis": (f"近期 {len(items)} 条 {insight_type} 洞察，但工作区没有任何 "
                          f"{kind} 监控在盯；目标取自洞察证据 {target}"),
            })
        proposals.append(run)
        covered.add(kind)                 # 同一轮不重复提同类型
    return proposals


# ================= 提案：自定义看板 =================

async def propose_boards(workspace_id: str) -> list[dict]:
    """按真实浏览行为起草"常用面板"看板（只提案）"""
    from ..core.store import get_store
    from .behavior import suggest_defaults
    from .custom_boards import list_boards, validate_panel_ids
    from .evolution import _in_cooldown

    store = await get_store()
    suggestion = await suggest_defaults(workspace_id, min_views=MIN_VIEWS_FOR_BOARD)
    panels = validate_panel_ids(suggestion.get("panel_ids") or [])
    if len(panels) < 2:
        return []                          # 一块面板不成看板
    boards = await list_boards(workspace_id)
    if any(set(b.get("panel_ids") or []) == set(panels) for b in boards):
        return []                          # 同一组面板换个顺序不算新看板
    board_id = "auto-frequent"
    runs = await store.list_evolution_runs(workspace_id, limit=200)
    if _in_cooldown(runs, "structure_board", board_id):
        return []
    existing = next((b for b in boards if b.get("id") == board_id), None)
    run = await store.create_evolution_run(
        workspace_id, "structure_board", target=board_id,
        before={"panel_ids": list((existing or {}).get("panel_ids") or [])},
        after={"id": board_id, "name": "常用面板（行为建议）", "panel_ids": panels},
        rationale={"source": "behavior_signals",
                   "top_views": suggestion.get("top_views"),
                   "basis": suggestion.get("basis", "")})
    return [run]


async def propose_structures(workspace_id: str) -> dict:
    """三类结构提案一次跑完（供调度/CLI/控制台按钮）"""
    dsl = await propose_dsl_rules(workspace_id)
    monitors = await propose_monitors(workspace_id)
    boards = await propose_boards(workspace_id)
    return {"structure_dsl": len(dsl), "structure_monitor": len(monitors),
            "structure_board": len(boards),
            "runs": [r["id"] for r in dsl + monitors + boards]}


# ================= 评测门 =================

async def gate_structure(workspace_id: str, run: dict) -> list[dict]:
    """结构提案的确定性检查；返回 checks 列表（由 evolution.evaluate_gate 汇总）"""
    from ..core.store import get_store
    from .dsl_models import persisted_rules, validate_dsl
    store = await get_store()
    kind = str(run.get("kind"))
    after = run.get("after") or {}
    checks: list[dict] = []

    if kind == "structure_dsl":
        spec = after.get("spec") or {}
        errors = validate_dsl(spec)
        checks.append({"name": "dsl_valid", "passed": not errors, "errors": errors})
        existing = {r.get("id") for r in await persisted_rules(workspace_id)}
        checks.append({"name": "rule_id_free",
                       "passed": spec.get("id") not in existing})
        series = await store.metric_series(workspace_id, str(spec.get("metric") or ""),
                                           days=90)
        checks.append({"name": "metric_samples", "passed": len(series) >= MIN_METRIC_POINTS,
                       "points": len(series)})
        # 阈值必须真的在分布尾部：否则新规则会天天报，制造告警疲劳
        values = [float(s["value"] or 0) for s in series]
        threshold = float((spec.get("condition") or {}).get("value") or 0)
        op = str((spec.get("condition") or {}).get("op") or ">=")
        hits = sum(1 for v in values
                   if (v >= threshold if op in (">=", ">") else v <= threshold))
        rate = hits / len(values) if values else 1.0
        checks.append({"name": "not_too_noisy", "passed": rate <= 0.2,
                       "hit_rate": round(rate, 3)})
    elif kind == "structure_monitor":
        from ..core.governor import CronTooFrequent, validate_cron
        from .monitors import MonitorService
        kind_name = str(after.get("kind") or "")
        checks.append({"name": "kind_supported",
                       "passed": kind_name in MonitorService.KINDS})
        checks.append({"name": "target_present",
                       "passed": bool(after.get("target"))})
        monitors = await store.list_monitors_full(workspace_id)
        dup = any(str(m.get("kind")) == kind_name
                  and dict(m.get("target_json") or {}) == dict(after.get("target") or {})
                  for m in monitors)
        checks.append({"name": "no_duplicate_monitor", "passed": not dup})
        try:
            validate_cron(kind_name, str(after.get("schedule_cron") or ""))
            cron_ok = True
        except (CronTooFrequent, ValueError):
            cron_ok = False
        checks.append({"name": "cron_within_governor", "passed": cron_ok})
    elif kind == "structure_board":
        from .custom_boards import MAX_PANELS, list_boards, validate_panel_ids
        panels = list(after.get("panel_ids") or [])
        valid = validate_panel_ids(panels)
        checks.append({"name": "panels_valid",
                       "passed": bool(valid) and valid == panels[:len(valid)],
                       "panels": valid})
        checks.append({"name": "panel_count_in_range",
                       "passed": 2 <= len(valid) <= MAX_PANELS})
        boards = await list_boards(workspace_id)
        clash = any(b.get("id") != after.get("id")
                    and set(b.get("panel_ids") or []) == set(valid) for b in boards)
        checks.append({"name": "no_identical_board", "passed": not clash})
    return checks


# ================= 生效 / 回滚 =================

async def apply_structure(workspace_id: str, run: dict) -> dict:
    """生效结构提案；返回 applied_ref（回滚时原样撤销）"""
    from .evolution import EvolutionError
    kind = str(run.get("kind"))
    after = run.get("after") or {}
    if kind == "structure_dsl":
        from .dsl_models import save_rule
        spec = after.get("spec") or {}
        await save_rule(workspace_id, spec)
        return {"kind": kind, "rule_id": spec.get("id")}
    if kind == "structure_monitor":
        from ..core.scheduler import get_scheduler
        from .monitors import MonitorService
        svc = MonitorService(workspace_id, scheduler=get_scheduler())
        mon = await svc.create(str(after.get("kind")), dict(after.get("target") or {}),
                               str(after.get("schedule_cron") or DEFAULT_MONITOR_CRON))
        return {"kind": kind, "monitor_id": mon["id"]}
    if kind == "structure_board":
        from .custom_boards import save_board
        board = await save_board(workspace_id, name=str(after.get("name") or "常用面板"),
                                 panel_ids=list(after.get("panel_ids") or []),
                                 board_id=str(after.get("id") or ""))
        return {"kind": kind, "board_id": board["id"]}
    raise EvolutionError(f"未知结构提案类型：{kind}")


async def rollback_structure(workspace_id: str, run: dict) -> dict:
    """撤销结构提案：删规则 / 删监控 / 删看板（按 applied_ref，删不掉就如实说）"""
    ref = dict((run.get("gate") or {}).get("applied_ref") or {})
    kind = str(run.get("kind"))
    if kind == "structure_dsl":
        from .dsl_models import remove_rule
        rule_id = str(ref.get("rule_id") or (run.get("after") or {}).get("spec", {}).get("id") or "")
        return {"removed_rule": rule_id, "ok": await remove_rule(workspace_id, rule_id)}
    if kind == "structure_monitor":
        from ..core.scheduler import get_scheduler
        from .monitors import MonitorService
        monitor_id = str(ref.get("monitor_id") or "")
        if not monitor_id:
            return {"ok": False, "detail": "账本里没有 monitor_id（可能是历史提案）"}
        svc = MonitorService(workspace_id, scheduler=get_scheduler())
        return {"removed_monitor": monitor_id, "ok": await svc.delete(monitor_id)}
    if kind == "structure_board":
        from .custom_boards import delete_board, save_board
        board_id = str(ref.get("board_id") or (run.get("after") or {}).get("id") or "")
        before_panels = list((run.get("before") or {}).get("panel_ids") or [])
        if before_panels:                 # 覆盖过旧看板 → 恢复旧面板，而不是删掉
            await save_board(workspace_id, name="常用面板（行为建议）",
                             panel_ids=before_panels, board_id=board_id)
            return {"restored_board": board_id, "ok": True}
        return {"removed_board": board_id,
                "ok": await delete_board(workspace_id, board_id)}
    return {"ok": False, "detail": f"未知结构提案类型：{kind}"}


def is_structure(kind: str) -> bool:
    return str(kind).startswith("structure_")


def now_iso() -> str:
    return datetime.now(UTC).isoformat()
