"""Insight Flow 监控 Collector 路由（M7：补全 keyword/brand_mention/journey）

统一管线：source 插件采集 → 指标归一入库（metrics 表）→ 模型评估 → 质量门 → 洞察
- keyword：GSC query 维度 → KeywordOpportunity 检测（捡漏窗口）
- brand_mention：GDELT → 讨论量时序
- journey：GA4 漏斗步 → journey_step 指标 → JourneyGap 断点模型
"""

import json
from datetime import UTC, datetime, timezone

from ..collectors.base import CollectContext
from ..core.entities import Insight, InsightAction, Metric
from ..core.files import EventBus
from ..core.security import get_vault
from ..core.store import get_store
from ..engine.quality_gates import get_quality_gates


def _vault_json(key: str) -> dict:
    raw = get_vault().get(key)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def _load_source_plugin(source_id: str):
    """从仓库 plugins/ 目录动态加载 source 插件（importlib，零打包耦合）"""
    import importlib.util
    from pathlib import Path

    root = Path(__file__).parent.parent.parent
    entry = root / "plugins" / "sources" / source_id / "entry.py"
    if not entry.exists():
        raise RuntimeError(f"source 插件不存在: {source_id}")
    spec = importlib.util.spec_from_file_location(f"if_plugin_{source_id}", str(entry))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.create_plugin()


def _require(target: dict, field: str, hint: str = "") -> str:
    value = target.get(field, "")
    if not value:
        raise ValueError(f"缺少必填字段 target.{field}" + (f"（{hint}）" if hint else ""))
    return value


def _window_key_from_ts(ts: str, fallback) -> str:
    """数据时间 → 幂等窗口键（按小时）；解析失败回落到当前小时"""
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).strftime("%Y-%m-%d-%H")
    except (ValueError, TypeError):
        return fallback.strftime("%Y-%m-%d-%H")


async def _save_metrics(workspace_id: str, rows: list[dict],
                        monitor_id: str = "") -> int:
    """指标时序入库（幂等：同 monitor 同小时窗口同指标 INSERT OR IGNORE）

    返回实际新增数（重复返回 0）——调度器重跑/手动+定时双触发不会产生重复快照。
    旧数据 window_key='' 不受唯一索引约束（部分索引 WHERE window_key != ''）。
    """
    store = await get_store()
    now = datetime.now(UTC)
    window_key = now.strftime("%Y-%m-%d-%H")
    inserted = 0
    for r in rows:
        row_ts = r.get("ts", now.isoformat())
        # 幂等窗口键由**数据时间**推导（不是当前时钟）：否则历史回填/补采会被
        # 误判为同窗口而丢数据（SQLite 静默忽略，MySQL 唯一键直接报错）
        window_key = r.get("window_key") or _window_key_from_ts(row_ts, now)
        cur = await store._execute(
            """INSERT OR IGNORE INTO metrics
               (id, workspace_id, entity_type, entity_id, metric, value, dim_json, ts, monitor_id, window_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (r.get("id") or datetime.now(UTC).strftime("%Y%m%d%H%M%S%f"),
             workspace_id, r.get("entity_type", "site"), r.get("entity_id", "main"),
             r["metric"], float(r["value"]),
             json.dumps(r.get("dim", {}), ensure_ascii=False), row_ts,
             monitor_id, window_key),
        )
        if cur.rowcount > 0:
            inserted += 1
    await store._db.commit()
    return inserted


async def _save_insights(workspace_id: str, drafts: list[dict]) -> int:
    """洞察草稿 → 质量门 → 入库"""
    store = await get_store()
    gates = get_quality_gates()
    saved = 0
    for d in drafts:
        actions = [
            InsightAction(action_type=a.get("action_type", "investigate"),
                          description=a.get("description", ""))
            for a in d.get("actions_json", []) if isinstance(a, dict)
        ]
        insight = Insight(
            workspace_id=workspace_id,
            type=d.get("type", "generic"),
            title=d.get("title", ""),
            summary=d.get("summary", ""),
            severity=d.get("severity", "medium"),
            confidence=d.get("confidence", 0.5),
            evidence_json=d.get("evidence_json", []),
            models_json=d.get("models_json", []),
            actions_json=actions,
            stage_tags_json=d.get("stage_tags_json", []),
        )
        report = gates.validate(insight)
        if not report.passed:
            EventBus(workspace_id).emit("insight.quality_gate_blocked", {
                "title": d.get("title"), "errors": report.errors,
            })
            continue
        s = await store.create_insight(insight)
        await _notify_insight(workspace_id, s.id)
        EventBus(workspace_id).emit("insight.created", {
            "insight_id": s.id, "type": s.type,
        })
        saved += 1
    return saved


class CollectorRouter:
    """监控类型 → collector 执行路由"""

    def __init__(self, workspace_id: str):
        self.workspace_id = workspace_id

    # ========== keyword（GSC query 维度 → 关键词机会）==========

    async def run_keyword(self, monitor_id: str, target: dict) -> dict:
        site = _require(target, "site", "先完成 GSC 授权（接入向导）")
        from .token_manager import TokenManager
        token = TokenManager(self.workspace_id).ensure_fresh("gsc")

        plugin = _load_source_plugin("gsc")
        result = await plugin.collect(CollectContext(
            workspace_id=self.workspace_id, monitor_id=monitor_id,
            config={
                "access_token": token,
                "site_url": site,
                "dimension": "query",
                "row_limit": target.get("limit", 50),
            },
        ))

        now = datetime.now(UTC)
        ctx_metrics = []
        for item in result.items:
            dim = {"key": item["key"], "ctr": item["ctr"], "position": item["position"]}
            ctx_metrics.append(_Metric_stub(workspace_id=self.workspace_id,
                                            entity_id=item["key"], dim=dim,
                                            value=item["impressions"], ts=now))
            await _save_metrics(self.workspace_id, monitor_id=monitor_id, rows=[{
                "entity_type": "keyword", "entity_id": item["key"],
                "metric": "gsc_impressions", "value": item["impressions"],
                "dim": dim, "ts": now.isoformat(),
            }])

        from .models.growth_models import KeywordOpportunityModel
        from .router import ModelContext
        drafts = await KeywordOpportunityModel().evaluate(
            ModelContext(workspace_id=self.workspace_id, metrics=ctx_metrics))
        created = await _save_insights(self.workspace_id, drafts)

        EventBus(self.workspace_id).emit("source.collected",
                                         {"source": "gsc", "kind": "keyword_monitor",
                                          "items": len(result.items)})
        return {"kind": "keyword", "queries": len(result.items),
                "insights_created": created}

    # ========== brand_mention（GDELT → 讨论量时序）==========

    async def run_brand_mention(self, monitor_id: str, target: dict) -> dict:
        query = _require(target, "query")
        max_records = target.get("max_records", 50)

        # GDELT 公开 API 免认证（配额由速率限制约束）
        import httpx
        params = {"query": query, "mode": "artlist",
                  "maxrecords": max_records, "format": "json"}
        async with httpx.AsyncClient() as client:
            resp = await client.get("https://api.gdeltproject.org/api/v2/doc/doc",
                                    params=params, timeout=30.0)
            resp.raise_for_status()
            items = resp.json().get("articles", [])

        mentions = len(items)
        now = datetime.now(UTC)
        await _save_metrics(self.workspace_id, monitor_id=monitor_id, rows=[{
            "entity_type": "topic", "entity_id": query,
            "metric": "brand_mention", "value": mentions,
            "dim": {"query": query, "domains": sorted({i.get("domain", "") for i in items})[:10]},
            "ts": now.isoformat(),
        }])

        created = 0
        if mentions > 20:  # 讨论量阈值 → 舆情信号洞察
            created = await _save_insights(self.workspace_id, [{
                "type": "brand_mention_spike",
                "title": f"舆情信号：「{query}」提及量 {mentions}",
                "summary": (f"监控到「{query}」在新闻面提及 {mentions} 次（阈值 20），"
                            "建议复核热度来源并评估跟进角度。"),
                "severity": "medium", "confidence": 0.65,
                "evidence_json": [{"type": "gdelt_mention", "query": query,
                                   "count": mentions}],
                "actions_json": [{"action_type": "investigate",
                                  "description": "复核热度来源与跟进角度"}],
                "stage_tags_json": ["S0", "S1", "voice"],
            }])

        return {"kind": "brand_mention", "mentions": mentions,
                "insights_created": created}

    # ========== topic（全域主题监测：多渠道聚合，面向超级个体）==========

    async def run_topic(self, monitor_id: str, target: dict) -> dict:
        """主题监测：搜索 + 新闻 + 论坛多渠道聚合 → 统一时间线 + 舆情洞察

        target: {
          "query": "品牌/话题/关键词",
          "channels": ["news", "reddit", "search"],   # 渠道清单（可选，默认 news）
          "sentiment": true                            # 是否做情绪概览
        }
        不要求任何自有网站——面向个人 IP / 品牌 / 话题 / 行业事件。
        """
        query = _require(target, "query")
        channels = target.get("channels") or ["news"]
        if not isinstance(channels, list):
            raise ValueError("target.channels 必须是数组")

        # 渠道采集（逐渠道容错：单渠道失败不影响整体）
        results: dict[str, list] = {}
        errors: dict[str, str] = {}
        if "news" in channels:
            try:
                results["news"] = await self._collect_gdelt(query, target)
            except Exception as e:
                errors["news"] = f"{type(e).__name__}: {e}"
        if "search" in channels:
            try:
                results["search"] = await self._collect_search(query, target)
            except Exception as e:
                errors["search"] = f"{type(e).__name__}: {e}"
        if "reddit" in channels:
            try:
                results["reddit"] = await self._collect_reddit(query, target)
            except Exception as e:
                errors["reddit"] = f"{type(e).__name__}: {e}"

        total = sum(len(v) for v in results.values())
        now = datetime.now(timezone.utc)

        # 情绪分布（G-3：舆情核心指标）
        from .sentiment import distribution as sentiment_distribution
        texts = []
        for items in results.values():
            for i in items:
                texts.append(f"{i.get('title', '')} {i.get('text', '')}".strip())
        sent = sentiment_distribution(texts) if texts else {
            "total": 0, "counts": {"positive": 0, "neutral": 0, "negative": 0},
            "ratios": {"positive": 0.0, "neutral": 0.0, "negative": 0.0},
            "avg_score": 0.0, "negative_examples": [],
        }

        # 各渠道提及量 + 情绪分布入库（统一时间线的基础）
        metric_rows = []
        for channel, items in results.items():
            metric_rows.append({
                "entity_type": "topic", "entity_id": query,
                "metric": f"topic_mentions_{channel}", "value": len(items),
                "dim": {"query": query, "channel": channel,
                        "domains": sorted({str(i.get("domain") or i.get("source") or "")
                                           for i in items})[:10]},
                "ts": now.isoformat(),
            })
        if sent["total"]:
            metric_rows.append({
                "entity_type": "topic", "entity_id": query,
                "metric": "topic_negative_ratio", "value": sent["ratios"]["negative"],
                "dim": {"query": query, "counts": sent["counts"]},
                "ts": now.isoformat(),
            })
            metric_rows.append({
                "entity_type": "topic", "entity_id": query,
                "metric": "topic_sentiment_score", "value": sent["avg_score"],
                "dim": {"query": query, "counts": sent["counts"]},
                "ts": now.isoformat(),
            })
        if metric_rows:
            await _save_metrics(self.workspace_id, monitor_id=monitor_id, rows=metric_rows)

        # 舆情洞察：概览 + 负面预警
        drafts: list[dict] = []
        if total >= target.get("min_mentions", 15):
            channel_desc = "、".join(f"{c} {len(results[c])}" for c in results)
            samples = [(i.get("title") or i.get("text") or "")[:60]
                       for items in results.values() for i in items[:3]][:6]
            sent_desc = (f"情绪：正 {sent['counts']['positive']} / 中 {sent['counts']['neutral']}"
                         f" / 负 {sent['counts']['negative']}（负向占比 {sent['ratios']['negative']:.0%}）")
            drafts.append({
                "type": "topic_digest",
                "title": f"「{query}」全域提及 {total} 条",
                "summary": (f"多渠道聚合：{channel_desc}。{sent_desc}。"
                            f"近期信号示例：{'; '.join(s for s in samples if s)}"),
                "severity": "medium", "confidence": 0.7,
                "evidence_json": [{
                    "type": "topic_aggregate", "query": query,
                    "channels": {c: len(v) for c, v in results.items()},
                    "sentiment": sent, "errors": errors,
                }],
                "actions_json": [
                    {"action_type": "investigate", "description": "复核高提及来源，评估跟进角度"},
                    {"action_type": "mflow.create_content",
                     "description": f"围绕「{query}」产出解读内容"},
                ],
                "stage_tags_json": ["S0", "S1", "voice"],
            })

        # 负面预警（G-3）：负向占比超阈值 → 高级别告警
        neg_ratio = sent["ratios"]["negative"]
        alert_threshold = target.get("negative_alert_ratio", 0.35)
        min_negative = target.get("negative_alert_min", 5)
        if (sent["counts"]["negative"] >= min_negative
                and neg_ratio >= alert_threshold):
            examples = "；".join(
                f"「{e['text'][:40]}」({','.join(e['hits'][:2])})"
                for e in sent["negative_examples"][:3])
            drafts.append({
                "type": "topic_negative_alert",
                "title": f"舆情负面预警：「{query}」负向占比 {neg_ratio:.0%}",
                "summary": (f"监测到 {sent['counts']['negative']} 条负面提及"
                            f"（占比 {neg_ratio:.0%}，阈值 {alert_threshold:.0%}）。"
                            f"典型负面：{examples}。建议立即响应并归档处置过程。"),
                "severity": "critical" if neg_ratio >= 0.5 else "high",
                "confidence": 0.8,
                "evidence_json": [{
                    "type": "negative_sentiment", "query": query,
                    "negative_count": sent["counts"]["negative"],
                    "negative_ratio": neg_ratio,
                    "threshold": alert_threshold,
                    "examples": sent["negative_examples"],
                    "channels": {c: len(v) for c, v in results.items()},
                }],
                "actions_json": [
                    {"action_type": "investigate", "description": "定位负面来源与传播路径"},
                    {"action_type": "openflow.automation",
                     "description": "触发负面舆情响应流程（分群+通知）"},
                    {"action_type": "mflow.create_content",
                     "description": "产出澄清/回应内容"},
                ],
                "stage_tags_json": ["S2", "voice", "crisis"],
            })
        if drafts:
            await _save_insights(self.workspace_id, drafts)

        from ..core.governor import emit_throttled
        emit_throttled(EventBus(self.workspace_id), "source.collected", {
            "source": "topic", "kind": "topic_monitor",
            "channels": {c: len(v) for c, v in results.items()},
            "sentiment": sent["ratios"],
        })
        return {"kind": "topic", "query": query,
                "channels": {c: len(v) for c, v in results.items()},
                "total_mentions": total, "errors": errors,
                "sentiment": sent,
                "insights_created": len(drafts)}

    async def _collect_gdelt(self, query: str, target: dict) -> list[dict]:
        import httpx
        params = {"query": query, "mode": "artlist",
                  "maxrecords": target.get("max_records", 50), "format": "json"}
        async with httpx.AsyncClient() as client:
            resp = await client.get("https://api.gdeltproject.org/api/v2/doc/doc",
                                    params=params, timeout=30.0)
            resp.raise_for_status()
            return [{"title": a.get("title", ""), "domain": a.get("domain", ""),
                     "url": a.get("url", ""), "seendate": a.get("seendate", "")}
                    for a in resp.json().get("articles", [])]

    async def _collect_search(self, query: str, target: dict) -> list[dict]:
        """搜索渠道：优先 serper（有凭据时），否则跳过"""
        vault_key = get_vault().get("serper_api_key") or target.get("serper_api_key", "")
        if not vault_key:
            raise RuntimeError("serper_api_key 未配置（搜索渠道跳过）")
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://google.serper.dev/search",
                json={"q": query, "num": target.get("num", 10)},
                headers={"X-API-KEY": vault_key, "Content-Type": "application/json"},
                timeout=30.0)
            resp.raise_for_status()
            return [{"title": i.get("title", ""), "domain": i.get("domain", ""),
                     "url": i.get("link", ""), "text": i.get("snippet", "")}
                    for i in resp.json().get("organic", [])]

    async def _collect_reddit(self, query: str, target: dict) -> list[dict]:
        """论坛渠道：Reddit 公开 JSON 端点（无需 OAuth，速率受限）"""
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://www.reddit.com/search.json",
                params={"q": query, "limit": target.get("reddit_limit", 25)},
                headers={"User-Agent": "InsightFlow/1.0"},
                timeout=30.0)
            resp.raise_for_status()
            children = resp.json().get("data", {}).get("children", [])
            return [{"title": c.get("data", {}).get("title", ""),
                     "domain": "reddit.com",
                     "url": "https://reddit.com" + c.get("data", {}).get("permalink", ""),
                     "text": c.get("data", {}).get("selftext", "")[:200]}
                    for c in children]

    # ========== journey（GA4 漏斗步 → 断点模型）==========

    async def run_journey(self, monitor_id: str, target: dict) -> dict:
        steps = target.get("steps", [])  # [{"name": "visit_pricing", "event": "page_view_pricing"}]
        if not steps:
            raise ValueError("journey 监控缺少 target.steps")
        property_id = _require(target, "property_id")
        from .token_manager import TokenManager
        token = TokenManager(self.workspace_id).ensure_fresh("ga4")

        plugin = _load_source_plugin("ga4")
        result = await plugin.collect(CollectContext(
            workspace_id=self.workspace_id, monitor_id=monitor_id,
            config={
                "access_token": token,
                "property_id": property_id,
                "metrics": ["eventCount"],
                "dimensions": ["eventName"],
                "row_limit": 100,
            },
        ))

        # eventName → 漏斗步映射
        event_to_step = {s["event"]: s["name"] for s in steps if s.get("event")}
        counts: dict[str, float] = {}
        for item in result.items:
            ev = item["dimensions"].get("eventName", "")
            step_name = event_to_step.get(ev)
            if step_name:
                counts[step_name] = counts.get(step_name, 0) + item["metrics"].get("eventCount", 0)

        # journey_step 指标入库 + 组装 JourneyGap 模型输入
        now = datetime.now(UTC)
        ctx_metrics = []
        for name, count in counts.items():
            ctx_metrics.append(_Metric_stub(workspace_id=self.workspace_id,
                                            entity_id="main", metric="journey_step",
                                            value=count, dim={"step_name": name, "step": count},
                                            ts=now))
            await _save_metrics(self.workspace_id, monitor_id=monitor_id, rows=[{
                "entity_type": "site", "entity_id": "main",
                "metric": "journey_step", "value": count,
                "dim": {"step_name": name, "step": count},
                "ts": now.isoformat(),
            }])

        from .models.growth_models import JourneyGapModel
        from .router import ModelContext
        drafts = await JourneyGapModel().evaluate(
            ModelContext(workspace_id=self.workspace_id, metrics=ctx_metrics))
        created = await _save_insights(self.workspace_id, drafts)

        return {"kind": "journey", "steps_collected": len(counts),
                "insights_created": created}

    def _require(self, target: dict, field: str, hint: str = "") -> str:
        return _require(target, field, hint)


def _Metric_stub(workspace_id: str, entity_id: str, value: float, ts,
                 dim: dict | None = None, metric: str = "gsc_impressions"):
    """Metric 构造（避免循环 import 的轻量封装）"""
    return Metric(workspace_id=workspace_id, entity_type="site", entity_id=entity_id,
                  metric=metric, value=float(value), dim_json=dim, ts=ts)


async def _notify_insight(workspace_id: str, insight_id: str) -> None:
    """洞察创建 → 订阅推送（失败静默，不阻断主流程）"""
    try:
        from .subscriptions import notify_new_insight
        await notify_new_insight(workspace_id, insight_id)
    except Exception:
        pass
