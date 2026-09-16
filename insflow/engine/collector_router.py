"""Insight Flow 监控 Collector 路由（M7：补全 keyword/brand_mention/journey）

统一管线：source 插件采集 → 指标归一入库（metrics 表）→ 模型评估 → 质量门 → 洞察
- keyword：GSC query 维度 → KeywordOpportunity 检测（捡漏窗口）
- brand_mention：GDELT → 讨论量时序
- journey：GA4 漏斗步 → journey_step 指标 → JourneyGap 断点模型
"""

import json
from datetime import datetime, timezone

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


async def _save_metrics(workspace_id: str, rows: list[dict]) -> int:
    """指标时序入库"""
    store = await get_store()
    now = datetime.now(timezone.utc).isoformat()
    for r in rows:
        await store._execute(
            """INSERT INTO metrics (id, workspace_id, entity_type, entity_id, metric, value, dim_json, ts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (r.get("id") or datetime.now(timezone.utc).strftime("%f"),
             workspace_id, r.get("entity_type", "site"), r.get("entity_id", "main"),
             r["metric"], float(r["value"]),
             json.dumps(r.get("dim", {}), ensure_ascii=False), r.get("ts", now)),
        )
    await store._db.commit()
    return len(rows)


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

        now = datetime.now(timezone.utc)
        ctx_metrics = []
        for item in result.items:
            dim = {"key": item["key"], "ctr": item["ctr"], "position": item["position"]}
            ctx_metrics.append(_Metric_stub(workspace_id=self.workspace_id,
                                            entity_id=item["key"], dim=dim,
                                            value=item["impressions"], ts=now))
            await _save_metrics(self.workspace_id, [{
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
        now = datetime.now(timezone.utc)
        await _save_metrics(self.workspace_id, [{
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
        now = datetime.now(timezone.utc)
        ctx_metrics = []
        for name, count in counts.items():
            ctx_metrics.append(_Metric_stub(workspace_id=self.workspace_id,
                                            entity_id="main", metric="journey_step",
                                            value=count, dim={"step_name": name, "step": count},
                                            ts=now))
            await _save_metrics(self.workspace_id, [{
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
    from ..core.entities import Metric
    return Metric(workspace_id=workspace_id, entity_type="site", entity_id=entity_id,
                  metric=metric, value=float(value), dim_json=dim, ts=ts)
