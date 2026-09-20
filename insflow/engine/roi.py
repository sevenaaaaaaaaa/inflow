"""客户 ROI（面向 OPC / 代运营：一个客户到底赚不赚）

口径（诚实标注，不用"估算收益"糊弄）：
- **产出**：有效动作数（14 天验证 verdict=effective）、显著结论数、验证总数
- **成本**：数据源成本（billing `cost_usd`，按采集配额估算）+ LLM 成本（`llm_cost_usd`，
  按 token 实价累计）+ 采集/接口调用次数
- **单位经济**：成本 / 有效动作（越低越好）；无有效动作时给 `null` 而不是编造

不做的：不折算"增量收入"（那需要客户的客单价与转化价值，属于商务口径），
只提供可核对的成本与验证结论，让 OPC 自己乘。
"""

from datetime import UTC, datetime


async def client_roi(workspace_id: str, *, months: int = 1) -> dict:
    from ..core.store import get_store
    from .billing import BillingManager

    store = await get_store()
    summary = await store.verification_summary(workspace_id)
    actions = await store.list_actions(workspace_id)
    verified_actions = sum(1 for a in actions
                           if getattr(a.state, "value", a.state) == "verified")
    effective = int(summary.get("effective") or 0)
    significant = int(summary.get("significant") or 0)

    mgr = BillingManager(workspace_id)
    usage = await mgr.usage_summary()
    u = usage.get("usage") or {}

    def _used(kind: str) -> float:
        return float((u.get(kind) or {}).get("used") or 0)

    source_cost = _used("cost_usd")
    llm_cost = _used("llm_cost_usd")
    total_cost = round(source_cost + llm_cost, 4)
    per_effective = round(total_cost / effective, 4) if effective else None

    return {
        "workspace_id": workspace_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "output": {
            "effective_actions": effective,
            "significant_results": significant,
            "verified_total": int(summary.get("total") or 0),
            "actions_verified": verified_actions,
            "actions_total": len(actions),
        },
        "cost": {
            "source_cost_usd": round(source_cost, 4),
            "llm_cost_usd": round(llm_cost, 4),
            "total_cost_usd": total_cost,
            "api_calls": _used("api_calls"),
            "agent_asks": _used("agent_asks"),
            "deep_reports": _used("deep_reports"),
        },
        "unit_economics": {
            "cost_per_effective_action_usd": per_effective,
            "note": ("成本口径：数据源按采集配额估算 + LLM 按 token 实价累计；"
                     "未折算增量收入（需客户客单价，属商务口径）"),
        },
        "plan": (usage.get("plan") or {}).get("name") if isinstance(usage.get("plan"), dict) else None,
    }
