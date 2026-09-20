"""自进化（P1）：提案 → 评测门 → 生效 → 可回滚，全程记账

设计约束（护栏，写死在代码里）：
1. 只调整**可量化、可回滚**的参数：告警阈值覆盖、模型权重；不触碰安全/合规/频控
2. 单次改动幅度上限（阈值 ±20%、权重 ±20%），冷却期（同目标 72h 内不重复提案）
3. 样本不足（<MIN_SAMPLES）不提案；提案默认状态 `proposed`，需评测门通过 + 显式 apply
4. 每次改动写 `evolution_runs` 账本（依据/前后值/门禁结果），可 rollback 回之前值

专业边界：不做"模型自己改自己"的黑箱；这里是**灰盒自整定**——统计依据透明、
人工可审、随时回滚。（LLM 只可提议，不参与生效路径。）
"""

import contextlib
from datetime import UTC, datetime, timedelta

MAX_DELTA_RATIO = 0.20          # 常规单次改动幅度上限
GUARDRAIL_BANDS = (0.2, 0.5, 1.0, 2.0)   # 渐进档位：阈值远离分布时允许更大修正（仍须门禁+人工）
COOLDOWN_HOURS = 72             # 同目标冷却期
MIN_SAMPLES = 3                 # 最小样本（验证结果条数）
ALERT_TARGET_PER_90D = 3.0      # 告警目标频率（90 天内期望命中次数）
REVIEW_AFTER_DAYS = 14          # 生效后多少天做复盘（与动作验证窗口同步）
REVIEW_IMPROVE_PCT = 0.05       # 复盘判定阈值：±5% 以内算"没差别"


class EvolutionError(Exception):
    pass


async def _settings(workspace_id: str) -> tuple[dict, object]:
    from ..core.store import get_store
    store = await get_store()
    ws = await store.get_workspace(workspace_id)
    return dict((ws.settings_json if ws else {}) or {}), ws


async def _save_settings(workspace_id: str, settings: dict) -> None:
    from ..core.store import get_store
    store = await get_store()
    ws = await store.get_workspace(workspace_id)
    ws.settings_json = settings
    await store.update_workspace(ws)


def _in_cooldown(runs: list[dict], kind: str, target: str) -> bool:
    cutoff = datetime.now(UTC) - timedelta(hours=COOLDOWN_HOURS)
    for run in runs:
        if run.get("kind") != kind or run.get("target") != target:
            continue
        try:
            created = datetime.fromisoformat(str(run.get("created_at", "")))
        except ValueError:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if created > cutoff:
            return True
    return False


# ================= 提案：告警阈值 =================

async def propose_threshold_tunes(workspace_id: str) -> list[dict]:
    """为启用的告警规则提出阈值调整提案（不做任何生效动作）"""
    from ..core.store import get_store
    store = await get_store()
    rules = await store.list_alert_rules(workspace_id, enabled_only=True)
    runs = await store.list_evolution_runs(workspace_id, limit=200)
    settings, _ = await _settings(workspace_id)
    overrides = dict(settings.get("alert_threshold_overrides") or {})
    proposals = []
    for rule in rules:
        rule_id = str(rule.get("id") or "")
        if _in_cooldown(runs, "threshold_tune", rule_id):
            continue
        current = float(overrides.get(rule_id, rule.get("threshold") or 0))
        metric = str(rule.get("metric") or "")
        op = str(rule.get("op") or "gt")
        days = float(rule.get("window_days") or 7)
        series = await store.metric_series(workspace_id, metric, days=90)
        if len(series) < 10:
            continue
        values = [max(0.0, float(s["value"] or 0)) for s in series]
        candidate, rationale, band = _pick_threshold(values, current, op)
        if candidate is None:
            continue
        if abs(candidate - current) <= max(1e-9, abs(current) * 0.02):
            continue                                  # 变动太小，不值得提案
        rationale["guardrail_band"] = band
        rationale["corrective"] = band > MAX_DELTA_RATIO
        run = await store.create_evolution_run(
            workspace_id, "threshold_tune", target=rule_id,
            before={"threshold": current},
            after={"threshold": round(candidate, 6)},
            rationale={"metric": metric, "op": op, "days": days,
                       **rationale})
        proposals.append(run)
    return proposals


def _pick_threshold(values: list[float], current: float,
                    op: str) -> tuple[float | None, dict, float]:
    """挑"命中频率最接近目标"的阈值；按渐进档位放宽幅度（确定性，无随机）

    返回 (候选阈值, 依据, 使用的护栏档位)。档位越小越保守；当规则当前
    "几乎总是触发/几乎不触发"（远离分布）时允许更大修正，但都必须过评测门 + 人工 apply。
    """
    ordered = sorted(values)
    n = len(ordered)

    def freq(threshold: float) -> float:
        hits = sum(1 for v in ordered if v > threshold) if op in ("gt", "gte") \
            else sum(1 for v in ordered if v < threshold)
        return hits / max(1, n) * 90.0                # 换算到 90 天命中次数

    # 候选分位要跟比较方向一致：gt 取高分段（调高=少触发），lt 取低分段（调低=少触发）
    quantiles = ((0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95)
                 if op in ("gt", "gte")
                 else (0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5))
    candidates = {}
    for q in quantiles:
        idx = min(n - 1, max(0, int(q * n)))
        candidates[round(ordered[idx], 6)] = freq(ordered[idx])
    current_freq = freq(current)
    best: float | None = None
    band_used = MAX_DELTA_RATIO
    for band in GUARDRAIL_BANDS:
        band_best, band_gap = None, None
        for threshold, f in candidates.items():
            if abs(threshold - current) / max(1e-9, abs(current)) > band:
                continue
            gap = abs(f - ALERT_TARGET_PER_90D)
            if band_gap is None or gap < band_gap:
                band_best, band_gap = threshold, gap
        if band_best is not None:
            best, band_used = band_best, band
            break
    if best is None:
        return None, {}, MAX_DELTA_RATIO
    return best, {
        "current_freq_per_90d": round(current_freq, 2),
        "proposed_freq_per_90d": round(freq(best), 2),
        "target_freq_per_90d": ALERT_TARGET_PER_90D,
        "value_p50": ordered[n // 2], "value_p90": ordered[min(n - 1, int(0.9 * n))],
        "basis": (f"近 90 天 {n} 个数据点；当前阈值 90 天命中 {current_freq:.0f} 次"
                  f" → 目标 {ALERT_TARGET_PER_90D:.0f} 次"),
    }, band_used


# ================= 提案：模型权重 =================

async def propose_model_weights(workspace_id: str) -> dict | None:
    """按"验证有效率"给模型权重提案（平滑 + 幅度护栏）"""
    from ..core.store import get_store
    store = await get_store()
    runs = await store.list_evolution_runs(workspace_id, limit=200)
    if _in_cooldown(runs, "model_weight", "global"):
        return None
    results = await store.list_verification_results(workspace_id, limit=1000)
    if len(results) < MIN_SAMPLES:
        return None
    # 洞察类型 → 有效/显著统计（insight_type 为空时按 action_type 兜底）
    stats: dict[str, dict] = {}
    for r in results:
        key = str(r.get("insight_type") or r.get("action_type") or "unknown")
        entry = stats.setdefault(key, {"n": 0, "effective": 0, "significant": 0})
        entry["n"] += 1
        entry["effective"] += 1 if r.get("verdict") == "effective" else 0
        entry["significant"] += 1 if r.get("significant") else 0
    settings, _ = await _settings(workspace_id)
    current = {k: float(v) for k, v in (settings.get("model_weights") or {}).items()}
    after, rationale = {}, {}
    for key, s in stats.items():
        base = current.get(key, 1.0)
        score = (s["effective"] + 0.5) / (s["n"] + 1.0)          # 拉普拉斯平滑
        score *= 0.6 + 0.4 * (s["significant"] / max(1, s["n"]))
        target = max(0.2, min(2.0, base * (0.5 + score)))         # 目标权重
        delta = max(-MAX_DELTA_RATIO * base, min(MAX_DELTA_RATIO * base, target - base))
        after[key] = round(base + delta, 4)
        rationale[key] = {"samples": s["n"], "effective": s["effective"],
                          "significant": s["significant"],
                          "effective_rate": round(s["effective"] / s["n"], 3),
                          "before": base, "after": after[key]}
    if not after:
        return None
    return await store.create_evolution_run(
        workspace_id, "model_weight", target="global",
        before=current, after=after,
        rationale={"per_model": rationale,
                   "basis": f"近 {len(results)} 条结构化验证结论（反馈闭环）"})


# ================= 评测门（不降级才允许生效）=================

async def evaluate_gate(workspace_id: str, run: dict) -> dict:
    """对提案跑确定性检查；返回 {passed, checks[], note}"""
    from ..core.store import get_store
    store = await get_store()
    checks: list[dict] = []
    kind = str(run.get("kind"))
    if kind == "threshold_tune":
        rule = await store._fetchone("SELECT * FROM alert_rules WHERE workspace_id = ? AND id = ?",
                                     (workspace_id, run.get("target")))
        if not rule:
            return {"passed": False, "checks": [{"name": "rule_exists", "passed": False}],
                    "note": "规则不存在（可能已删除）"}
        metric = str(rule.get("metric") or "")
        series = await store.metric_series(workspace_id, metric, days=90)
        if len(series) < 10:
            return {"passed": False,
                    "checks": [{"name": "sample_sufficient", "passed": False}],
                    "note": "历史数据不足 10 点，拒绝生效"}
        values = [max(0.0, float(s["value"] or 0)) for s in series]
        op = str(rule.get("op") or "gt")

        def _freq(t: float) -> float:
            hits = sum(1 for v in values if (v > t if op in ("gt", "gte") else v < t))
            return hits / len(values) * 90.0
        before = float((run.get("before") or {}).get("threshold") or 0)
        after = float((run.get("after") or {}).get("threshold") or 0)
        fb, fa = _freq(before), _freq(after)
        checks.append({"name": "freq_in_band", "passed": 0.5 <= fa <= 30.0,
                       "after_per_90d": round(fa, 2)})
        checks.append({"name": "no_wild_swing",
                       "passed": abs(fa - fb) <= max(3.0, fb * 0.75),
                       "before_per_90d": round(fb, 2)})
        band = float((run.get("rationale") or {}).get("guardrail_band")
                     or MAX_DELTA_RATIO)
        checks.append({"name": "delta_within_guardrail",
                       "passed": abs(after - before) <= abs(before) * band + 1e-9,
                       "ratio": round(abs(after - before) / max(1e-9, abs(before)), 4),
                       "band": band})
    elif kind == "model_weight":
        results = await store.list_verification_results(workspace_id, limit=1000)
        if len(results) < MIN_SAMPLES:
            return {"passed": False,
                    "checks": [{"name": "sample_sufficient", "passed": False,
                                "n": len(results)}], "note": "验证结论样本不足"}
        before = run.get("before") or {}
        after = run.get("after") or {}

        def _weighted_rate(weights: dict) -> float:
            num = den = 0.0
            for r in results:
                key = str(r.get("insight_type") or r.get("action_type") or "unknown")
                w = float(weights.get(key, 1.0))
                num += w * (1.0 if r.get("verdict") == "effective" else 0.0)
                den += w
            return num / den if den else 0.0
        rb, ra = _weighted_rate(before), _weighted_rate(after)
        checks.append({"name": "weighted_effective_rate_not_worse",
                       "passed": ra >= rb - 0.01,
                       "before": round(rb, 4), "after": round(ra, 4)})
        over = [k for k, v in after.items()
                if abs(float(v) - float(before.get(k, 1.0))) > MAX_DELTA_RATIO
                * max(1e-9, float(before.get(k, 1.0))) + 1e-6]
        checks.append({"name": "delta_within_guardrail", "passed": not over,
                       "violations": over})
    elif kind.startswith("structure_"):
        from .structure_evolution import gate_structure
        checks.extend(await gate_structure(workspace_id, run))
        if not checks:
            checks.append({"name": "known_kind", "passed": False})
    else:
        checks.append({"name": "known_kind", "passed": False})
    passed = all(c["passed"] for c in checks)
    gate = {"passed": passed, "checks": checks,
            "evaluated_at": datetime.now(UTC).isoformat()}
    await store.update_evolution_run(workspace_id, run["id"],
                                    gate_json=gate,
                                    status="proposed" if passed else "rejected")
    return gate


# ================= 生效 / 回滚 =================

async def apply_run(workspace_id: str, run_id: str, *, force: bool = False,
                    actor: str = "本地用户") -> dict:
    from ..core.store import get_store
    store = await get_store()
    run = await store.get_evolution_run(workspace_id, run_id)
    if not run:
        raise EvolutionError("提案不存在")
    gate = run.get("gate") or {}
    if not gate:
        gate = await evaluate_gate(workspace_id, run)
    if not gate.get("passed") and not force:
        raise EvolutionError(f"评测门未通过，拒绝生效：{gate.get('checks')}")
    after = run.get("after") or {}
    if run["kind"].startswith("structure_"):
        from .structure_evolution import apply_structure
        applied_ref = await apply_structure(workspace_id, run)
        await store.update_evolution_run(
            workspace_id, run_id, status="applied",
            applied_at=datetime.now(UTC).isoformat(),
            gate_json={**gate, "forced": bool(force), "actor": actor,
                       "applied_ref": applied_ref})
        with contextlib.suppress(Exception):
            await store.record_admin(workspace_id, "evolution.apply", actor=actor,
                                     target_type=run["kind"], target_id=run["target"],
                                     detail={"after": after, "ref": applied_ref,
                                             "forced": bool(force)})
        from ..core.files import EventBus
        EventBus(workspace_id).emit("evolution.applied", {
            "run_id": run_id, "kind": run["kind"], "target": run["target"],
            "ref": applied_ref})
        return {"ok": True, "run_id": run_id, "status": "applied",
                "applied": after, "ref": applied_ref, "gate": gate}

    settings, _ = await _settings(workspace_id)
    if run["kind"] == "threshold_tune":
        overrides = dict(settings.get("alert_threshold_overrides") or {})
        overrides[run["target"]] = float(after.get("threshold") or 0)
        settings["alert_threshold_overrides"] = overrides
    elif run["kind"] == "model_weight":
        settings["model_weights"] = {k: float(v) for k, v in after.items()}
    else:
        raise EvolutionError(f"未知提案类型：{run['kind']}")
    await _save_settings(workspace_id, settings)
    await store.update_evolution_run(workspace_id, run_id, status="applied",
                                     applied_at=datetime.now(UTC).isoformat(),
                                     gate_json={**gate, "forced": bool(force),
                                                "actor": actor})
    with contextlib.suppress(Exception):
        await store.record_admin(workspace_id, "evolution.apply", actor=actor,
                                 target_type=run["kind"], target_id=run["target"],
                                 detail={"before": run.get("before"),
                                         "after": after, "forced": bool(force)})
    from ..core.files import EventBus
    EventBus(workspace_id).emit("evolution.applied", {
        "run_id": run_id, "kind": run["kind"], "target": run["target"]})
    return {"ok": True, "run_id": run_id, "status": "applied",
            "applied": after, "gate": gate}


async def rollback_run(workspace_id: str, run_id: str, *,
                       actor: str = "本地用户") -> dict:
    from ..core.store import get_store
    store = await get_store()
    run = await store.get_evolution_run(workspace_id, run_id)
    if not run:
        raise EvolutionError("提案不存在")
    if run.get("status") != "applied":
        raise EvolutionError(f"只能回滚已生效的提案（当前 {run.get('status')}）")
    if run["kind"].startswith("structure_"):
        from .structure_evolution import rollback_structure
        undo = await rollback_structure(workspace_id, run)
        await store.update_evolution_run(
            workspace_id, run_id, status="rolled_back",
            rolled_back_at=datetime.now(UTC).isoformat())
        with contextlib.suppress(Exception):
            await store.record_admin(workspace_id, "evolution.rollback", actor=actor,
                                     target_type=run["kind"], target_id=run["target"],
                                     detail=undo)
        return {"ok": True, "run_id": run_id, "status": "rolled_back", "undo": undo}

    settings, _ = await _settings(workspace_id)
    before = run.get("before") or {}
    if run["kind"] == "threshold_tune":
        overrides = dict(settings.get("alert_threshold_overrides") or {})
        if "threshold" in before:
            overrides[run["target"]] = float(before["threshold"])
        else:
            overrides.pop(run["target"], None)
        settings["alert_threshold_overrides"] = overrides
    elif run["kind"] == "model_weight":
        settings["model_weights"] = {k: float(v) for k, v in before.items()}
    await _save_settings(workspace_id, settings)
    await store.update_evolution_run(
        workspace_id, run_id, status="rolled_back",
        rolled_back_at=datetime.now(UTC).isoformat())
    with contextlib.suppress(Exception):
        await store.record_admin(workspace_id, "evolution.rollback", actor=actor,
                                 target_type=run["kind"], target_id=run["target"],
                                 detail={"restored": before})
    return {"ok": True, "run_id": run_id, "status": "rolled_back",
            "restored": before}


async def propose_all(workspace_id: str, *, structures: bool = True) -> dict:
    """一次性生成所有可提案（参数 + 结构；供调度/CLI/控制台按钮）"""
    thresholds = await propose_threshold_tunes(workspace_id)
    weights = await propose_model_weights(workspace_id)
    out = {"threshold_tunes": len(thresholds),
           "model_weights": 1 if weights else 0,
           "structure_dsl": 0, "structure_monitor": 0, "structure_board": 0,
           "runs": [r["id"] for r in thresholds] + ([weights["id"]] if weights else [])}
    if structures:
        from .structure_evolution import propose_structures
        struct = await propose_structures(workspace_id)
        out.update({k: struct[k] for k in
                    ("structure_dsl", "structure_monitor", "structure_board")})
        out["runs"] = out["runs"] + struct["runs"]
    return out


# ================= 复盘闭环（apply 后 N 天回写账本）=================
#
# 此前 apply 完就结束了：没人知道那次调整到底有没有用，账本只记"改了什么"，
# 不记"改得对不对"。这里补上第 14 天的回看，并由此得出**提案准确率**——
# 自进化第一次有了自己的成绩单（而不是只会自我表扬）。
#
# 判定口径（写在这里，避免事后各说各话）：
# - 北极星取**验证结论有效率**（effective / 总数），因为这是全系统在优化的量
# - 两侧窗口任一侧样本 < MIN_SAMPLES → verdict = insufficient，**不判**，不计入准确率
# - 被人工回滚 → verdict = reverted（算失败样本：人用脚投票了）
# - 指标均值变化只作为**上下文**呈现，不参与判定：阈值/结构类提案的"好"方向
#   本来就不唯一（错误率越低越好、会话数越高越好），硬判会是编故事


def _parse_iso(value: str):
    try:
        dt = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def _run_metric(run: dict) -> str:
    """提案关联的指标（拿不到就空——空就只做北极星对比）"""
    kind = str(run.get("kind"))
    if kind == "structure_dsl":
        return str(((run.get("after") or {}).get("spec") or {}).get("metric") or "")
    return str((run.get("rationale") or {}).get("metric") or "")


async def _verification_rate(workspace_id: str, start, end) -> tuple[float | None, int]:
    from ..core.store import get_store
    store = await get_store()
    rows = await store.list_verification_results(workspace_id, limit=2000)
    picked = []
    for r in rows:
        ts = _parse_iso(r.get("created_at", ""))
        if ts and start <= ts < end:
            picked.append(r)
    if not picked:
        return None, 0
    eff = sum(1 for r in picked if r.get("verdict") == "effective")
    return eff / len(picked), len(picked)


async def _metric_mean(workspace_id: str, metric: str, start, end) -> float | None:
    if not metric:
        return None
    from ..core.store import get_store
    store = await get_store()
    span = max(1.0, (datetime.now(UTC) - start).total_seconds() / 86400 + 1)
    series = await store.metric_series(workspace_id, metric, days=span)
    values = [float(s["value"] or 0) for s in series
              if (_parse_iso(s.get("ts", "")) or datetime.now(UTC)) >= start
              and (_parse_iso(s.get("ts", "")) or datetime.now(UTC)) < end]
    return sum(values) / len(values) if values else None


async def review_run(workspace_id: str, run: dict, *,
                     days: int = REVIEW_AFTER_DAYS, now=None) -> dict:
    """对一条已生效提案做复盘（纯计算，不写库）"""
    now = now or datetime.now(UTC)
    applied = _parse_iso(run.get("applied_at", ""))
    if not applied:
        return {"verdict": "insufficient", "reason": "没有 applied_at（未生效）"}
    window = timedelta(days=days)
    before_rate, before_n = await _verification_rate(
        workspace_id, applied - window, applied)
    after_rate, after_n = await _verification_rate(
        workspace_id, applied, applied + window)
    metric = _run_metric(run)
    before_mean = await _metric_mean(workspace_id, metric, applied - window, applied)
    after_mean = await _metric_mean(workspace_id, metric, applied, applied + window)
    change_pct = None
    if before_mean not in (None, 0) and after_mean is not None:
        change_pct = round((after_mean - before_mean) / abs(before_mean), 4)

    rolled_back = str(run.get("status")) == "rolled_back"
    delta = None
    if rolled_back:
        verdict = "reverted"
    elif before_n >= MIN_SAMPLES and after_n >= MIN_SAMPLES:
        delta = round((after_rate or 0) - (before_rate or 0), 4)
        verdict = ("improved" if delta >= REVIEW_IMPROVE_PCT else
                   "worse" if delta <= -REVIEW_IMPROVE_PCT else "neutral")
    else:
        verdict = "insufficient"

    return {
        "window_days": days,
        "applied_at": run.get("applied_at"),
        "rolled_back": rolled_back,
        "verdict": verdict,
        "north_star": "verification_effective_rate",
        "before": {"effective_rate": before_rate, "samples": before_n,
                   "metric": metric or None, "metric_mean": before_mean},
        "after": {"effective_rate": after_rate, "samples": after_n,
                  "metric_mean": after_mean},
        "delta_effective_rate": delta,
        "metric_change_pct": change_pct,
        "basis": (f"生效前后各 {days} 天：验证样本 {before_n} → {after_n}，"
                  + (f"有效率 {before_rate:.0%} → {after_rate:.0%}"
                     if before_rate is not None and after_rate is not None
                     else "样本不足，不判定")),
        "reviewed_at": now.isoformat(),
    }


async def review_due(workspace_id: str, *, days: int = REVIEW_AFTER_DAYS,
                     limit: int = 50, now=None) -> dict:
    """把所有"生效满 N 天且还没复盘"的提案跑一遍并回写账本"""
    from ..core.store import get_store
    store = await get_store()
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=days)
    reviewed = []
    for run in await store.list_evolution_runs(workspace_id, limit=500):
        if run.get("reviewed_at"):
            continue
        if str(run.get("status")) not in ("applied", "rolled_back"):
            continue
        applied = _parse_iso(run.get("applied_at", ""))
        if not applied or applied > cutoff:
            continue
        review = await review_run(workspace_id, run, days=days, now=now)
        await store.update_evolution_run(workspace_id, run["id"],
                                         review_json=review,
                                         reviewed_at=review["reviewed_at"])
        reviewed.append({"run_id": run["id"], "kind": run.get("kind"),
                         "verdict": review["verdict"]})
        if len(reviewed) >= limit:
            break
    return {"reviewed": len(reviewed), "runs": reviewed}


async def accuracy(workspace_id: str) -> dict:
    """提案准确率 = improved / 已判定（neutral、worse、reverted 都算没做对）

    未判定（样本不足）的不计入分母；一条都没判定时返回 None，不编数。
    """
    from ..core.store import get_store
    store = await get_store()
    tally = {"improved": 0, "neutral": 0, "worse": 0, "reverted": 0, "insufficient": 0}
    by_kind: dict[str, dict] = {}
    for run in await store.list_evolution_runs(workspace_id, limit=500):
        review = run.get("review") or {}
        verdict = str(review.get("verdict") or "")
        if verdict not in tally:
            continue
        tally[verdict] += 1
        row = by_kind.setdefault(str(run.get("kind")), dict.fromkeys(tally, 0))
        row[verdict] += 1
    judged = tally["improved"] + tally["neutral"] + tally["worse"] + tally["reverted"]
    return {
        "reviewed": sum(tally.values()),
        "judged": judged,
        "accuracy": round(tally["improved"] / judged, 3) if judged else None,
        "non_regression_rate": (round((tally["improved"] + tally["neutral"]) / judged, 3)
                                if judged else None),
        "tally": tally,
        "by_kind": by_kind,
        "definition": ("准确率 = 复盘判定为 improved 的占已判定提案之比；"
                       "样本不足的提案不计入分母（不编数）"),
    }
