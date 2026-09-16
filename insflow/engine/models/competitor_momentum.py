"""内置洞察模型 - 竞品动量分析"""

from datetime import datetime, timezone
from typing import Any

from ..router import InsightModel, ModelContext


class CompetitorMomentumModel(InsightModel):
    """竞品动量分析模型

    监测竞品的价格变动、产品更新、市场活动等信号，
    产出竞品异动洞察。
    """

    @property
    def id(self) -> str:
        return "competitor_momentum"

    @property
    def name(self) -> str:
        return "竞品动量分析"

    @property
    def description(self) -> str:
        return "监测竞品异动信号（定价/产品/市场活动）"

    @property
    def required_metrics(self) -> list[str]:
        return ["competitor_mention", "competitor_pricing"]

    async def evaluate(self, ctx: ModelContext) -> list[dict]:
        """评估竞品数据"""
        insights = []

        # 分析竞品提及
        mention_metrics = [m for m in ctx.metrics if m.metric == "competitor_mention"]
        for metric in mention_metrics:
            entity_id = metric.entity_id
            value = metric.value

            # 如果提及量突然增加
            if value > 10:  # 阈值可配置
                insights.append({
                    "type": "competitor_move",
                    "title": f"竞品 {entity_id} 讨论量上升",
                    "summary": f"竞品 {entity_id} 在社交媒体/论坛上的讨论量达到 {value}，可能有新动作",
                    "severity": "medium",
                    "confidence": 0.6,
                    "evidence_json": [
                        {
                            "type": "mention_volume",
                            "competitor": entity_id,
                            "count": value,
                            "ts": metric.ts.isoformat(),
                        }
                    ],
                    "actions_json": [
                        {
                            "action_type": "investigate",
                            "description": f"调查 {entity_id} 的最新动态",
                        },
                        {
                            "action_type": "mflow.create_content",
                            "description": "创建竞品对比内容",
                        },
                    ],
                    "stage_tags_json": ["S0", "S1", "competitor"],
                })

        # 分析竞品定价变动
        pricing_metrics = [m for m in ctx.metrics if m.metric == "competitor_pricing"]
        for metric in pricing_metrics:
            entity_id = metric.entity_id
            dim = metric.dim_json

            # 检查价格变动
            if dim.get("changed"):
                old_price = dim.get("old_price", 0)
                new_price = dim.get("new_price", 0)
                if old_price > 0:
                    change_pct = (new_price - old_price) / old_price
                    severity = "high" if abs(change_pct) > 0.15 else "medium"

                    insights.append({
                        "type": "competitor_pricing",
                        "title": f"竞品 {entity_id} 价格变动",
                        "summary": f"竞品 {entity_id} 价格从 ${old_price} 调整为 ${new_price}（{change_pct:+.0%}）",
                        "severity": severity,
                        "confidence": 0.9,
                        "evidence_json": [
                            {
                                "type": "pricing_change",
                                "competitor": entity_id,
                                "old_price": old_price,
                                "new_price": new_price,
                                "change_pct": change_pct,
                                "ts": metric.ts.isoformat(),
                            }
                        ],
                        "actions_json": [
                            {
                                "action_type": "investigate",
                                "description": "分析竞品定价策略变化原因",
                            },
                            {
                                "action_type": "openflow.automation",
                                "description": "触发定价审查流程",
                            },
                        ],
                        "stage_tags_json": ["S3", "pricing"],
                    })

        return insights
