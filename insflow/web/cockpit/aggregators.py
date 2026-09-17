"""Insight Flow 驾驶舱数据聚合（9 舱）

性能守则（学 OpenFlow）：
- 全部走 TTL 缓存（默认 90s）：页面刷新不重复打库
- 只用聚合查询（metric_series/totals/breakdown）+ 限额（LIMIT），不做全表扫描
- 洞察类数据统一从差量接口取（limit ≤ 300），Python 侧聚合
"""

from datetime import datetime, timedelta, timezone

from ...core.cache import cache
from ...core.files import EventBus, ReportStore
from ...core.store import get_store

TTL = 90.0  # 驾驶舱缓存秒数


def _key(cockpit: str, workspace_id: str, days: float) -> str:
    return f"cockpit:{cockpit}:{workspace_id}:{days}"


def _within(items, days: float):
    since = datetime.now(timezone.utc) - timedelta(days=days)
    return [i for i in items if i.created_at >= since]


def _count_by(items, attr: str) -> dict:
    out: dict[str, int] = {}
    for i in items:
        k = getattr(i, attr, None)
        k = k.value if hasattr(k, "value") else str(k)
        out[k] = out.get(k, 0) + 1
    return out


# ================= C1 情报总览 =================

async def overview(workspace_id: str, days: float = 7) -> dict:
    async def build():
        store = await get_store()
        insights = await store.list_insights(workspace_id, limit=300)
        recent = _within(insights, days)
        alerts = [i for i in recent if "negative" in i.type or i.type.endswith("_alert")]
        events = EventBus(workspace_id).read(limit=200)
        ok = sum(1 for e in events if e["type"] == "monitor.run_finished")
        fail = sum(1 for e in events if e["type"] == "monitor.alert")

        risk = await store.metric_breakdown(workspace_id, "topic_negative_ratio", days=days * 3)
        monitors = await store.list_monitors_full(workspace_id)
        return {
            "kpis": {
                "new": len([i for i in insights if i.status.value == "new"]),
                "recent": len(recent),
                "alerts": len(alerts),
                "verified": _count_by(insights, "status").get("verified", 0),
                "collect_ok": ok, "collect_fail": fail,
            },
            "timeline": [{"ts": i.created_at.isoformat(), "title": i.title,
                          "severity": i.severity.value,
                          "meta": f"{i.type} · 置信度 {i.confidence:.0%}"}
                         for i in recent[:15]],
            "risk": [(r["entity_id"], r["value"]) for r in risk[:8]],
            "loop": _count_by(insights, "status"),
            "monitors": monitors[:12],
        }
    return await cache.get_or_compute(_key("overview", workspace_id, days), build, TTL)


# ================= C2 舆情 =================

async def sentiment(workspace_id: str, days: float = 7) -> dict:
    async def build():
        store = await get_store()
        neg_series = await store.metric_series(workspace_id, "topic_negative_ratio",
                                               days=days, agg="avg")
        score_series = await store.metric_series(workspace_id, "topic_sentiment_score",
                                                 days=days, agg="avg")
        totals = await store.metric_totals(workspace_id, [
            "topic_mentions_news", "topic_mentions_search", "topic_mentions_reddit",
        ], days=days * 4)
        topics = await store.metric_breakdown(workspace_id, "topic_negative_ratio",
                                              days=days * 4)
        insights = [i for i in await store.list_insights(workspace_id, limit=300)
                    if "sentiment" in i.type or "negative" in i.type or i.type == "topic_digest"]
        alerts = [i for i in insights if i.type == "topic_negative_alert"]

        # 高频负向词（从预警证据聚合）
        word_freq: dict[str, int] = {}
        for ins in alerts:
            for ev in ins.evidence_json or []:
                for ex in (ev.get("examples") or []):
                    for hit in ex.get("hits", []):
                        word_freq[hit] = word_freq.get(hit, 0) + 1

        mentions_total = sum(v["value"] for v in totals.values())
        neg_total = sum(int((i.evidence_json or [{}])[0].get("negative_count", 0))
                        for i in alerts) if alerts else 0
        return {
            "kpis": {
                "mentions": mentions_total,
                "negative": neg_total,
                "avg_negative": (sum(p["value"] for p in neg_series) / len(neg_series)
                                 if neg_series else 0.0),
                "alerts": len(alerts),
            },
            "trend": {
                "labels": [p["bucket"][5:] for p in neg_series],
                "negative_ratio": [p["value"] for p in neg_series],
                "score": [p["value"] for p in score_series],
            },
            "channels": [(k.replace("topic_mentions_", ""), v["value"],
                          ("var(--accent)", "var(--ok)", "var(--warn)",
                           "var(--danger)")[i % 4])
                         for i, (k, v) in enumerate(totals.items())],
            "topics": [(r["entity_id"], r["value"]) for r in topics[:10]],
            "alerts": alerts[:10],
            "words": sorted(word_freq.items(), key=lambda kv: -kv[1])[:25],
        }
    return await cache.get_or_compute(_key("sentiment", workspace_id, days), build, TTL)


# ================= C3 流量 =================

async def traffic(workspace_id: str, days: float = 14) -> dict:
    async def build():
        store = await get_store()
        clicks = await store.metric_series(workspace_id, "gsc_clicks", days=days)
        sessions = await store.metric_series(workspace_id, "ga4_sessions", days=days)
        conversions = await store.metric_series(workspace_id, "ga4_conversions", days=days)
        totals = await store.metric_totals(
            workspace_id, ["gsc_clicks", "gsc_impressions", "gsc_ctr",
                           "ga4_sessions", "ga4_conversions"], days=days)
        cwv = await store.latest_metrics(workspace_id, [
            "crux_lcp", "crux_inp", "crux_cls", "crux_ttfb"])
        insights = await store.list_insights(workspace_id, limit=300)
        anomalies = [i for i in insights
                     if i.type in ("traffic_anomaly", "conversion_low", "keyword_opportunity")]
        # 关键词机会（从洞察证据取，散点：搜索量 × 排名）
        scatter = []
        for ins in insights:
            if ins.type != "keyword_opportunity":
                continue
            ev = (ins.evidence_json or [{}])[0]
            scatter.append((float(ev.get("impressions", 0)),
                            float(ev.get("position", 0)), str(ev.get("keyword", ""))[:16]))
        return {
            "kpis": {
                "clicks": totals.get("gsc_clicks", {}).get("value", 0),
                "impressions": totals.get("gsc_impressions", {}).get("value", 0),
                "ctr": totals.get("gsc_ctr", {}).get("value", 0),
                "sessions": totals.get("ga4_sessions", {}).get("value", 0),
                "conversions": totals.get("ga4_conversions", {}).get("value", 0),
            },
            "trend": {
                "labels": [p["bucket"][5:] for p in (clicks or sessions)],
                "clicks": [p["value"] for p in clicks],
                "sessions": [p["value"] for p in sessions],
            },
            "conversions": [p["value"] for p in conversions],
            "cwv": {m["metric"]: float(m["value"]) for m in cwv},
            "scatter": scatter[:60],
            "anomalies": anomalies[:10],
        }
    return await cache.get_or_compute(_key("traffic", workspace_id, days), build, TTL)


# ================= C4 竞品 =================

async def competitor(workspace_id: str, days: float = 14) -> dict:
    async def build():
        store = await get_store()
        profiles = await store.list_competitors(workspace_id)
        insights = await store.list_insights(workspace_id, limit=300)
        comp_types = ("competitor_pricing", "site_change", "keyword_gap")
        signals = [i for i in insights if i.type in comp_types]
        recent = _within(signals, days)
        # 关键词缺口（从 keyword_gap 证据聚合）
        gaps: list[tuple[str, float]] = []
        for ins in insights:
            if ins.type != "keyword_gap":
                continue
            ev = (ins.evidence_json or [{}])[0]
            for kw in (ev.get("top_keywords") or []):
                gaps.append((str(kw.get("keyword", ""))[:20], float(kw.get("volume", 0))))
        return {
            "kpis": {"competitors": len(profiles), "signals": len(recent),
                     "gaps": len(gaps)},
            "profiles": profiles[:12],
            "pricing": [(p.get("name") or p.get("domain", ""),
                         len([i for i in recent if (p.get("domain", "") in i.summary)]))
                        for p in profiles[:10]],
            "gaps": gaps[:15],
            "timeline": [{"ts": i.created_at.isoformat(), "title": i.title,
                          "severity": i.severity.value, "meta": i.type}
                         for i in recent[:15]],
        }
    return await cache.get_or_compute(_key("competitor", workspace_id, days), build, TTL)


# ================= C5 旅程 & RFM =================

async def journey(workspace_id: str, days: float = 30) -> dict:
    async def build():
        store = await get_store()
        rows = await store.latest_metrics(workspace_id, ["journey_step", "ga4_retention"])
        funnel: list[tuple[str, float]] = []
        retention: list[dict] = []
        for r in rows:
            if r["metric"] == "journey_step":
                dim = r["dim_json"] or {}
                funnel.append((str(dim.get("step_name", r["entity_id"])), float(r["value"])))
            elif r["metric"] == "ga4_retention":
                retention.append({"bucket": str(r["ts"])[5:10], "value": float(r["value"])})
        insights = await store.list_insights(workspace_id, limit=300)
        gaps = [i for i in insights if i.type in ("journey_gap", "journey_content_gap")]
        at_risk = [i for i in insights if i.type == "rfm_at_risk"]
        return {
            "kpis": {"steps": len(funnel), "gaps": len(gaps), "at_risk": len(at_risk)},
            "funnel": funnel,
            "retention": sorted(retention, key=lambda x: x["bucket"]),
            "gaps": gaps[:12],
            "at_risk": at_risk[:6],
        }
    return await cache.get_or_compute(_key("journey", workspace_id, days), build, TTL)


# ================= C6 行动与验证 =================

async def action_loop(workspace_id: str, days: float = 30) -> dict:
    async def build():
        store = await get_store()
        actions = await store.list_actions(workspace_id)
        feedback = await store.get_feedback_stats(workspace_id)
        effectiveness = await store.get_model_effectiveness(workspace_id)
        due = await store.list_actions_due_for_verification()
        events = EventBus(workspace_id).read(limit=300)
        act_events = [e for e in events if e["type"].startswith("action.")]
        return {
            "kpis": {
                "total": len(actions),
                "dispatched": sum(1 for a in actions if a.state.value == "dispatched"),
                "verifying": sum(1 for a in actions if a.state.value == "verifying"),
                "verified": sum(1 for a in actions if a.state.value == "verified"),
                "dead": sum(1 for a in actions if a.state.value == "dead"),
            },
            "verdicts": feedback,
            "effectiveness": effectiveness[:10],
            "due": due[:10],
            "timeline": [{"ts": e["ts"], "title": e.get("payload", {}).get("action_type",
                                                                          e["type"]),
                          "severity": "info", "meta": e["type"]}
                         for e in act_events[-15:]][::-1],
        }
    return await cache.get_or_compute(_key("action_loop", workspace_id, days), build, TTL)


# ================= C7 报告中心 =================

async def reports(workspace_id: str) -> dict:
    async def build():
        store = ReportStore(workspace_id)
        cats = ("weekly", "diagnosis", "competitors", "maturity", "verification",
                "deep-dive", "invoices")
        out = {}
        for c in cats:
            files = store.list_reports(c)
            out[c] = [{"name": p.name, "size": p.stat().st_size,
                       "mtime": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc)
                       .strftime("%Y-%m-%d %H:%M")} for p in files[:20]]
        return {"categories": out,
                "total": sum(len(v) for v in out.values())}
    return await cache.get_or_compute(_key("reports", workspace_id, 0), build, TTL)


# ================= C8 运行监控 =================

async def ops(workspace_id: str, days: float = 7) -> dict:
    async def build():
        from datetime import datetime as _dt
        events = EventBus(workspace_id).read(limit=500)
        ok = sum(1 for e in events if e["type"] == "monitor.run_finished")
        fail = sum(1 for e in events if e["type"] == "monitor.alert")
        alerts = [e for e in events if e["type"] in (
            "quota.exceeded", "source.token_refresh_failed", "monitor.alert",
            "action.dead", "subscription.push_failed")]

        from ...core.security import get_quota_ledger
        quota = get_quota_ledger().get_all_usage()
        from ...engine.token_manager import TokenManager
        tokens = TokenManager(workspace_id).audit_all()
        # 备份状态
        from pathlib import Path
        backups = []
        try:
            from ...core.files import DATA_DIR
            bdir = Path(DATA_DIR).parent / "data-backup"
            if bdir.exists():
                backups = sorted([d.name for d in bdir.iterdir() if d.is_dir()],
                                 reverse=True)[:3]
        except Exception:
            pass
        return {
            "kpis": {"ok": ok, "fail": fail,
                     "success_rate": (ok / (ok + fail)) if (ok + fail) else None,
                     "alerts": len(alerts)},
            "quota": [{"source": k, **v} for k, v in list(quota.items())[:10]],
            "tokens": tokens,
            "alerts": [{"ts": e["ts"], "title": e["type"],
                        "severity": "critical" if e["type"] in
                        ("quota.exceeded", "source.token_refresh_failed") else "high",
                        "meta": str(e.get("payload", {}))[:80]}
                       for e in alerts[-15:]][::-1],
            "backups": backups,
        }
    return await cache.get_or_compute(_key("ops", workspace_id, days), build, TTL)


# ================= C9 商业化 =================

async def billing(workspace_id: str, days: float = 30) -> dict:
    async def build():
        from ...engine.billing import BillingManager
        mgr = BillingManager(workspace_id)
        summary = await mgr.usage_summary()
        trial = await mgr.trial_status()
        usage = summary["usage"]
        return {
            "plan": summary, "trial": trial,
            "gauges": [
                ("API 调用", (usage["api_calls"]["pct"] or 0)),
                ("Agent 问答", (usage["agent_asks"]["pct"] or 0)),
                ("深度报告", (usage["deep_reports"]["pct"] or 0)),
                ("数据源费用", (usage["cost_usd"]["pct"] or 0)),
            ],
            "usage": {k: v for k, v in usage.items() if isinstance(v, dict)},
        }
    return await cache.get_or_compute(_key("billing", workspace_id, days), build, TTL)


COCKPITS = {
    "overview": overview,
    "sentiment": sentiment,
    "traffic": traffic,
    "competitor": competitor,
    "journey": journey,
    "action-loop": action_loop,
    "reports": reports,
    "ops": ops,
    "billing": billing,
}
