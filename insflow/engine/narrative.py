"""自动洞察叙事 + 自然语言问数（确定性优先，LLM 可选增强）

两条路径，保证「没有 LLM 也可用」：
1. **确定性叙事**（narrate）：指标关键变化 + 异常 + 维度贡献 + 数据质量 + 建议动作，
   所有数字都来自查询，可逐条溯源（口径/血缘/数据质量状态）。
2. **LLM 润色**（可选）：把确定性草稿交给 LLMGateway 改写成管理者可读的摘要；
   prompt 明确「只能使用给定数字，不得新增事实」，避免 LLM 编数字。

问数（ask_metrics）：把自然语言里的指标名/维度/时间范围解析成结构化查询并执行，
不依赖 LLM（关键词+别名匹配），LLM 不可用时仍能回答"上周会话量怎么样"。
"""

import re

RANGE_WORDS = [
    (r"(最近|近|过去)\s*(\d{1,3})\s*天", lambda m: float(m.group(2))),
    (r"(最近|近|过去)\s*(\d{1,2})\s*周", lambda m: float(m.group(2)) * 7),
    (r"(最近|近|过去)\s*(\d{1,2})\s*个?月", lambda m: float(m.group(2)) * 30),
    (r"(今天|今日)", lambda m: 1.0),
    (r"(昨天|昨日)", lambda m: 2.0),
    (r"(上周|上星期)", lambda m: 14.0),
    (r"(本月|这个月)", lambda m: 30.0),
]
TREND_WORDS = ("趋势", "走势", "变化", "曲线", "trend")
TOP_WORDS = ("最高", "最多", "排名", "top", "占比", "构成", "分布")
ANOMALY_WORDS = ("异常", "突变", "波动")
DQ_WORDS = ("数据质量", "新鲜度", "断更", "缺数", "停更", "sla")
ATTR_WORDS = ("归因", "渠道贡献", "哪个渠道")
LIFT_WORDS = ("增量", "效果", "验证", "提升多少")


def parse_question(question: str, known_metrics: list[str]) -> dict:
    """NL → 结构化（指标/天数/意图/维度）；纯规则，无 LLM"""
    q = (question or "").strip()
    days = 30.0
    for pattern, fn in RANGE_WORDS:
        m = re.search(pattern, q)
        if m:
            days = float(fn(m))
            break
    metric = ""
    lowered = q.lower()
    for name in sorted(known_metrics, key=len, reverse=True):   # 长名优先，避免子串误配
        if name.lower() in lowered:
            metric = name
            break
    intent = "value"
    if any(w in q for w in TREND_WORDS):
        intent = "trend"
    if any(w in q for w in TOP_WORDS):
        intent = "top"
    if any(w in q for w in ANOMALY_WORDS):
        intent = "anomaly"
    if any(w in lowered for w in DQ_WORDS) or any(w in q for w in DQ_WORDS):
        intent = "data_quality"
    if any(w in q for w in ATTR_WORDS):
        intent = "attribution"
    if any(w in q for w in LIFT_WORDS):
        intent = "lift"
    dim = ""
    for candidate in ("province", "country", "channel", "device", "campaign",
                      "step_name", "source"):
        if candidate in lowered or {"province": "地域", "country": "国家",
                                    "channel": "渠道", "device": "设备",
                                    "campaign": "计划", "step_name": "步骤",
                                    "source": "来源"}[candidate] in q:
            dim = candidate
            break
    return {"question": q, "metric": metric, "days": days, "intent": intent, "dim": dim}


async def ask_metrics(workspace_id: str, question: str) -> dict:
    """规则解析 + 执行（无 LLM 也能答）；返回 {answer, facts, intent, citations}"""
    from ..core.store import get_store
    store = await get_store()
    catalog = await store.metric_catalog(workspace_id, days=365)
    defs = await store.list_metric_defs(workspace_id)
    known = [c["metric"] for c in catalog] + [d["name"] for d in defs]
    parsed = parse_question(question, known)
    metric, days, intent = parsed["metric"], parsed["days"], parsed["intent"]

    if intent == "data_quality":
        from .data_quality import check_workspace
        dq = await check_workspace(workspace_id, window_days=int(max(7, days)))
        s = dq["summary"]
        detail = "；".join(f"{i['metric']} {i['status']}({i['age_hours']}h)"
                          for i in dq["items"][:5])
        return {"intent": intent, "answer": (
            f"数据质量（近 {int(max(7, days))} 天窗口）：{s['metrics']} 个指标，"
            f"新鲜 {s['fresh']}、延迟 {s['late']}、停更 {s['stale']}、缺失 {s['missing']}，"
            f"{s['with_gaps']} 个有缺口。示例：{detail}"),
            "facts": {"summary": s},
            "citations": [{"type": "data_quality", "window_days": int(max(7, days))}]}

    if intent == "attribution":
        from .attribution import channel_credit
        attr = await channel_credit(workspace_id, days=days, method="linear")
        top = "、".join(f"{c['channel']} {c['share']}" for c in attr["channels"][:5])
        return {"intent": intent, "answer": (
            f"渠道归因（linear，近 {int(days)} 天"
            + ("，⚠️ 无触点序列，已降级为渠道量占比" if attr["degraded"] else "")
            + f"）：{top or '暂无数据'}"),
            "facts": attr,
            "citations": [{"type": "attribution", "method": attr["method"]}]}

    if intent == "lift":
        from .attribution import lift_summary
        lift = await lift_summary(workspace_id, days=days)
        rows = [a for a in lift["actions"] if not a.get("insufficient_data")][:3]
        detail = "；".join(
            f"{a['action_type']} {a['metric']} {a.get('lift_pct') and format(a['lift_pct'], '+.1%')}"
            f"{'（显著）' if a['significant'] else '（不显著）'}" for a in rows) or "暂无可评估动作"
        return {"intent": intent, "answer": (
            f"动作增量（近 {int(days)} 天，共 {lift['total']} 个可评估，显著 {lift['significant']}）：{detail}。"
            "注意：非随机实验，不能据此宣布因果。"),
            "facts": lift, "citations": [{"type": "action_lift"}]}

    if not metric:
        hint = "、".join(known[:8]) if known else "（暂无指标）"
        return {"intent": intent, "answer": (
            f"没听懂要查哪个指标。可用指标示例：{hint}；"
            "也可以问「数据质量怎么样」「渠道归因」「动作增量如何」。"),
            "facts": {"known_metrics": known[:20]}, "citations": []}

    if intent == "trend" or intent == "anomaly":
        narr = await narrate(workspace_id, metric, days=days)
        return {"intent": intent, "answer": narr["markdown"],
                "facts": narr["facts"], "citations": narr["citations"]}

    if intent == "top":
        dim = parsed["dim"] or "channel"
        rows = await store.metric_dim_breakdown(workspace_id, metric, dim, days=days,
                                                limit=10)
        if not rows:
            return {"intent": intent, "answer": f"{metric} 在 {dim} 维度暂无数据。",
                    "facts": {}, "citations": [{"type": "metric", "name": metric}]}
        total = sum(r["value"] for r in rows) or 1.0
        top = "、".join(f"{r['key']} {r['value']:,.0f}（{r['value'] / total:.0%}）"
                        for r in rows[:5])
        return {"intent": intent, "answer": f"{metric} 按 {dim}（近 {int(days)} 天）：{top}",
                "facts": {"dim": dim, "rows": rows[:10]},
                "citations": [{"type": "metric", "name": metric, "dim": dim}]}

    value = await store.metric_total(workspace_id, metric, days=days)
    prev = await store.metric_total(workspace_id, metric, days=days * 2)
    prev_window = prev - value
    delta = (value - prev_window) / prev_window if prev_window else None
    answer = (f"{metric} 近 {int(days)} 天为 {value:,.0f}"
              + (f"，环比上期 {delta:+.1%}" if delta is not None else "，暂无可比上期"))
    return {"intent": "value", "answer": answer,
            "facts": {"metric": metric, "value": value, "previous": prev_window,
                      "delta": delta},
            "citations": [{"type": "metric", "name": metric, "days": days}]}


async def narrate(workspace_id: str, metric: str, *, days: float = 30,
                  dim: str = "", polish: bool = False) -> dict:
    """确定性叙事：结论 / 证据 / 数据质量 / 维度贡献 / 建议 / 口径溯源"""
    from ..core.store import get_store
    from ..viz.charts import anomaly_points_robust, forecast_series
    store = await get_store()
    from .semantics import SemanticError, resolve_metric
    kind = "base"
    lineage = None
    try:
        resolved = await resolve_metric(workspace_id, metric, days=days)
        series = [p["value"] for p in resolved["series"]]
        labels = [p["bucket"] for p in resolved["series"]]
        kind, lineage = resolved["kind"], resolved["lineage"]
    except SemanticError:
        rows = await store.metric_series(workspace_id, metric, days=days)
        series = [r["value"] for r in rows]
        labels = [r["bucket"] for r in rows]
    if not series:
        return {"markdown": f"**{metric}** 近 {int(days)} 天没有数据（可查数据质量）。",
                "facts": {"metric": metric}, "citations": []}

    total = sum(series)
    last, first = series[-1], series[0]
    delta = (last - first) / first if first else None
    _, _, bad = anomaly_points_robust(series)
    preds, band = forecast_series(series, 7)
    top_rows = []
    for candidate in (dim, "channel", "province", "country", "device"):
        if not candidate:
            continue
        rows = await store.metric_dim_breakdown(workspace_id, metric, candidate,
                                                days=days, limit=5)
        if rows:
            top_rows = [(candidate, r["key"], r["value"]) for r in rows]
            break

    from .data_quality import check_workspace
    dq = await check_workspace(workspace_id, window_days=int(max(7, days)))
    dq_item = next((i for i in dq["items"] if i["metric"] == metric), None)

    lines = [f"### {metric}（近 {int(days)} 天）", ""]
    lines.append(f"- **结论**：合计 {total:,.0f}；最新 {last:,.0f}"
                 + (f"，较窗口起点 {delta:+.1%}" if delta is not None else ""))
    if bad:
        pts = "、".join(f"{labels[i]}({series[i]:,.0f})" for i in bad[-3:] if i < len(labels))
        lines.append(f"- **异常点**（MAD 稳健检测）：{pts}")
    if preds:
        lines.append(f"- **趋势外推**（linear，非预测承诺）：未来 7 期 "
                     f"{preds[0]:,.0f} → {preds[-1]:,.0f}"
                     f"（±{band[0]:,.0f} 带宽）")
    if top_rows:
        d_name, _, _ = top_rows[0]
        top_total = sum(v for _, _, v in top_rows) or 1
        lines.append(f"- **维度贡献**（{d_name}）："
                     + "、".join(f"{k} {v / top_total:.0%}" for _, k, v in top_rows))
    if dq_item:
        lines.append(f"- **数据质量**：{dq_item['status']}"
                     f"（最近 {dq_item['age_hours']}h，SLA {dq_item['sla_hours']}h"
                     + (f"，缺口 {dq_item['gap_count']} 天" if dq_item["gap_count"] else "")
                     + f"；续采方式 {dq_item['recoverable']}）")
    lines.append(f"- **建议**：{'先排查数据缺口' if (dq_item and dq_item['status'] != 'fresh') else '复核异常点并派发动作验证'}；"
                 "派发后在「行动验证」看 14 天增量。")
    lines.append("")
    lines.append(f"_口径：{kind}" + (f"；表达式 `{lineage['expr']}`" if lineage and lineage.get('expr') else "")
                 + (f"；责任人 {lineage['owner']}" if lineage and lineage.get('owner') else "")
                 + "_")
    markdown = "\n".join(lines)

    if polish:
        markdown = await _polish(markdown, workspace_id)
    return {"markdown": markdown, "facts": {
        "metric": metric, "days": days, "total": total, "last": last,
        "delta": delta, "anomalies": [labels[i] for i in bad if i < len(labels)],
        "forecast": preds[:3], "dim_top": top_rows,
        "data_quality": dq_item},
        "citations": [{"type": "metric", "name": metric},
                      {"type": "semantics", "kind": kind}]}


async def _polish(markdown: str, workspace_id: str) -> str:
    """LLM 润色（可选）：只允许使用给定数字，避免编造"""
    try:
        import os

        from ..agent.llm import LLMGateway
        gw = LLMGateway(api_key=os.environ.get("OPENAI_API_KEY") or None)
        if not gw.available:
            return markdown
        out = await gw.chat([
            {"role": "system", "content":
             "你是增长分析师。把下面的确定性结论改写成 3-5 句管理者摘要。"
             "严格禁止引入未出现的数字或事实；无法判断时保留原文表述。"},
            {"role": "user", "content": markdown}])
        text = (out or {}).get("content") if isinstance(out, dict) else str(out)
        return f"{text}\n\n---\n{markdown}" if text else markdown
    except Exception:
        return markdown


async def auto_insight(workspace_id: str, metric: str, *, days: float = 30) -> dict:
    """自动洞察草稿（可经质量门写入洞察流）"""
    narr = await narrate(workspace_id, metric, days=days)
    facts = narr["facts"]
    delta = facts.get("delta")
    severity = "high" if (delta is not None and abs(delta) >= 0.3) else \
        "medium" if (delta is not None and abs(delta) >= 0.15) else "low"
    title = f"{metric} " + ("上升" if (delta or 0) > 0 else "下降" if delta else "观察")
    return {"type": "narrative_metric", "title": title[:80], "severity": severity,
            "confidence": 0.7 if facts.get("anomalies") else 0.6,
            "summary": narr["markdown"].split("\n")[2].lstrip("- ") if narr["markdown"] else "",
            "evidence_json": [{"demo": False, "kind": "narrative", "metric": metric,
                               "facts": {k: v for k, v in facts.items()
                                         if k != "data_quality"}}],
            "actions_json": [{"action_type": "investigate",
                              "description": f"复核 {metric} 变化并评估行动"}],
            "markdown": narr["markdown"]}
