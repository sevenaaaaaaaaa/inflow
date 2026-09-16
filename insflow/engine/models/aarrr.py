"""内置洞察模型 - AARRR 流量诊断"""


from ..router import InsightModel, ModelContext


class AARRRModel(InsightModel):
    """AARRR 流量诊断模型

    分析 Acquisition（获取）、Activation（激活）、Retention（留存）、
    Revenue（收入）、Referral（推荐）五个维度的流量数据，
    产出流量异常洞察。
    """

    @property
    def id(self) -> str:
        return "aarrr"

    @property
    def name(self) -> str:
        return "AARRR 流量诊断"

    @property
    def description(self) -> str:
        return "分析 AARRR 五维流量数据，识别异常和机会"

    @property
    def required_metrics(self) -> list[str]:
        return ["traffic", "conversion", "retention"]

    async def evaluate(self, ctx: ModelContext) -> list[dict]:
        """评估流量数据"""
        insights = []

        # 分析流量趋势
        traffic_metrics = [m for m in ctx.metrics if m.metric == "traffic"]
        if traffic_metrics:
            # 检查流量下降
            recent = sorted(traffic_metrics, key=lambda m: m.ts, reverse=True)[:7]
            if len(recent) >= 2:
                latest = recent[0].value
                previous = recent[1].value
                if previous > 0:
                    change_pct = (latest - previous) / previous
                    if change_pct < -0.2:  # 下降超过 20%
                        insights.append({
                            "type": "traffic_anomaly",
                            "title": "流量显著下降",
                            "summary": f"最近流量较前一期下降 {abs(change_pct):.0%}",
                            "severity": "high",
                            "confidence": 0.8,
                            "evidence_json": [
                                {
                                    "type": "metric_trend",
                                    "metric": "traffic",
                                    "before": previous,
                                    "after": latest,
                                    "change_pct": change_pct,
                                }
                            ],
                            "actions_json": [
                                {
                                    "action_type": "investigate",
                                    "description": "检查流量来源变化，排查是否有渠道异常",
                                },
                                {
                                    "action_type": "openflow.automation",
                                    "description": "在 OpenFlow 中创建流量监控任务",
                                },
                            ],
                            "stage_tags_json": ["S1", "acquisition"],
                        })

        # 分析转化率
        conversion_metrics = [m for m in ctx.metrics if m.metric == "conversion"]
        if conversion_metrics:
            recent = sorted(conversion_metrics, key=lambda m: m.ts, reverse=True)[:7]
            if recent:
                avg_conversion = sum(m.value for m in recent) / len(recent)
                if avg_conversion < 0.02:  # 转化率低于 2%
                    insights.append({
                        "type": "conversion_low",
                        "title": "转化率偏低",
                        "summary": f"平均转化率为 {avg_conversion:.2%}，低于行业基准",
                        "severity": "medium",
                        "confidence": 0.7,
                        "evidence_json": [
                            {
                                "type": "metric_average",
                                "metric": "conversion",
                                "value": avg_conversion,
                                "benchmark": 0.02,
                            }
                        ],
                        "actions_json": [
                            {
                                "action_type": "mflow.create_content",
                                "description": "创建转化优化相关内容",
                            },
                        ],
                        "stage_tags_json": ["S1", "activation"],
                    })

        return insights
