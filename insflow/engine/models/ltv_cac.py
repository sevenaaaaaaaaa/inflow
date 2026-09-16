"""内置洞察模型 - LTV:CAC 健康度分析"""

from ..router import InsightModel, ModelContext


class LTCACModel(InsightModel):
    """LTV:CAC 健康度模型

    LTV:CAC < 3 → 获客效率告警；> 5 → 加大投放的机会窗口。
    """

    @property
    def id(self) -> str:
        return "ltv_cac"

    @property
    def name(self) -> str:
        return "LTV:CAC 健康度"

    @property
    def description(self) -> str:
        return "获客成本与客户生命周期价值比值健康度（SaaS 基准 3:1）"

    @property
    def required_metrics(self) -> list[str]:
        return ["ltv", "cac"]

    async def evaluate(self, ctx: ModelContext) -> list[dict]:
        insights = []
        ltv_metrics = [m for m in ctx.metrics if m.metric == "ltv"]
        cac_metrics = [m for m in ctx.metrics if m.metric == "cac"]

        if not ltv_metrics or not cac_metrics:
            return insights

        latest_ltv = max(ltv_metrics, key=lambda m: m.ts).value
        latest_cac = max(cac_metrics, key=lambda m: m.ts).value
        if latest_cac <= 0:
            return insights

        ratio = latest_ltv / latest_cac

        if ratio < 3.0:
            insights.append({
                "type": "ltv_cac_unhealthy",
                "title": f"LTV:CAC 比值偏低（{ratio:.1f}:1）",
                "summary": (
                    f"LTV ${latest_ltv:.0f} / CAC ${latest_cac:.0f} = {ratio:.1f}:1，"
                    "低于健康基准 3:1。优先检查渠道 ROI 与留存，而非加大投放。"
                ),
                "severity": "high" if ratio < 2.0 else "medium",
                "confidence": 0.85,
                "evidence_json": [{
                    "type": "ratio_calc", "ltv": latest_ltv, "cac": latest_cac,
                    "ratio": round(ratio, 2), "benchmark": 3.0,
                }],
                "actions_json": [
                    {"action_type": "investigate",
                     "description": "分渠道核算 CAC，暂停 ROI 为负的渠道"},
                    {"action_type": "mflow.create_content",
                     "description": "产出留存/复购导向的运营内容"},
                ],
                "stage_tags_json": ["S1", "S3", "unit_economics"],
            })
        elif ratio > 5.0:
            insights.append({
                "type": "ltv_cac_opportunity",
                "title": f"LTV:CAC 比值优异（{ratio:.1f}:1），可加大投放",
                "summary": (
                    f"LTV:CAC = {ratio:.1f}:1 远超 3:1 基准，存在加大获客投入的机会窗口。"
                ),
                "severity": "medium",
                "confidence": 0.75,
                "evidence_json": [{
                    "type": "ratio_calc", "ltv": latest_ltv, "cac": latest_cac,
                    "ratio": round(ratio, 2), "benchmark": 5.0,
                }],
                "actions_json": [
                    {"action_type": "openflow.automation",
                     "description": "扩量前建立渠道级 ROI 看板与预算熔断"},
                ],
                "stage_tags_json": ["S1", "unit_economics"],
            })

        return insights
