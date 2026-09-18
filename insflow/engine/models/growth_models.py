"""内置洞察模型 - 关键词机会 / 留存健康 / 定价异动 / NPS / 旅程缺口"""


from ..router import InsightModel, ModelContext


class KeywordOpportunityModel(InsightModel):
    """关键词机会模型（GSC 曝光高但点击低 = 排名贴近首页的捡漏窗口）"""

    @property
    def id(self) -> str:
        return "keyword_opportunity"

    @property
    def name(self) -> str:
        return "关键词机会"

    @property
    def description(self) -> str:
        return "识别高曝光低 CTR 的关键词（排名 5-20 位捡漏窗口）"

    @property
    def required_metrics(self) -> list[str]:
        return ["gsc_impressions"]

    async def evaluate(self, ctx: ModelContext) -> list[dict]:
        insights = []
        for m in ctx.metrics:
            if m.metric != "gsc_impressions":
                continue
            dim = m.dim_json if isinstance(m.dim_json, dict) else {}
            ctr = float(dim.get("ctr", 0))
            position = float(dim.get("position", 99))
            keyword = str(dim.get("key", m.entity_id))
            impressions = m.value

            # 曝光 >500、排名 5-20、CTR 低于该位次基准 → 机会
            if impressions >= 500 and 5 <= position <= 20 and ctr < 0.08:
                expected_ctr = max(0.02, 0.30 / position)
                if ctr < expected_ctr * 0.6:
                    insights.append({
                        "type": "keyword_opportunity",
                        "title": f"关键词机会：{keyword}",
                        "summary": (
                            f"「{keyword}」曝光 {impressions:.0f} 次、排名第 {position:.0f}，"
                            f"但 CTR 仅 {ctr:.1%}（基准约 {expected_ctr:.1%}）。"
                            "优化标题/描述或补一篇针对性内容即可捡漏。"
                        ),
                        "severity": "medium",
                        "confidence": 0.7,
                        "evidence_json": [{
                            "type": "gsc_query", "keyword": keyword,
                            "impressions": impressions, "position": position,
                            "ctr": ctr, "expected_ctr": round(expected_ctr, 4),
                        }],
                        "actions_json": [
                            {"action_type": "mflow.create_content",
                             "description": f"围绕「{keyword}」产出/改写针对性内容"},
                            {"action_type": "openflow.webhook_insight",
                             "description": "推送洞察到 OpenFlow CDP"},
                        ],
                        "stage_tags_json": ["S1", "seo"],
                    })
        return insights


class RetentionHealthModel(InsightModel):
    """留存健康模型（留存连续下滑 → 流失预警 + 唤醒动作）"""

    @property
    def id(self) -> str:
        return "retention_health"

    @property
    def name(self) -> str:
        return "留存健康"

    @property
    def description(self) -> str:
        return "监测留存率趋势，连续下滑触发流失预警"

    @property
    def required_metrics(self) -> list[str]:
        return ["ga4_retention"]

    async def evaluate(self, ctx: ModelContext) -> list[dict]:
        insights = []
        series = sorted([m for m in ctx.metrics if m.metric == "ga4_retention"],
                        key=lambda m: m.ts)
        values = [m.value for m in series]
        if len(values) >= 4:
            recent = values[-3:]
            drops = [recent[i] < recent[i - 1] for i in range(1, len(recent))]
            if all(drops) and (recent[-1] - values[0]) / values[0] < -0.1:
                insights.append({
                    "type": "retention_decline",
                    "title": "留存率连续下滑",
                    "summary": (
                        f"最近 3 期留存率连续下降（{values[0]:.0%} → {recent[-1]:.0%}），"
                        "疑似产品体验或内容质量变化导致流失。"
                    ),
                    "severity": "high",
                    "confidence": 0.8,
                    "evidence_json": [{
                        "type": "retention_trend", "series": [round(v, 4) for v in values[-6:]],
                    }],
                    "actions_json": [
                        {"action_type": "investigate",
                         "description": "定位流失集中的用户群与行为断点"},
                        {"action_type": "openflow.automation",
                         "description": "在 OpenFlow 创建流失预警分群与唤醒旅程"},
                    ],
                    "stage_tags_json": ["S2", "retention"],
                })
        return insights


class PricingWatchModel(InsightModel):
    """定价异动模型（竞品/自身定价信号）"""

    @property
    def id(self) -> str:
        return "pricing_watch"

    @property
    def name(self) -> str:
        return "定价异动"

    @property
    def description(self) -> str:
        return "监测竞品定价页变动信号，产出应对建议"

    @property
    def required_metrics(self) -> list[str]:
        return ["competitor_pricing"]

    async def evaluate(self, ctx: ModelContext) -> list[dict]:
        insights = []
        for m in ctx.metrics:
            if m.metric != "competitor_pricing":
                continue
            dim = m.dim_json if isinstance(m.dim_json, dict) else {}
            if not dim.get("changed"):
                continue
            entity = m.entity_id
            old_p, new_p = float(dim.get("old_price", 0)), float(dim.get("new_price", 0))
            if old_p <= 0:
                continue
            pct = (new_p - old_p) / old_p
            direction = "涨价" if pct > 0 else "降价"
            insights.append({
                "type": "competitor_pricing",
                "title": f"竞品 {entity} {direction} {abs(pct):.0%}",
                "summary": (
                    f"竞品 {entity} 定价从 ${old_p:.0f} 调整为 ${new_p:.0f}（{pct:+.0%}）。"
                    + ("我方可在内容中强调性价比与差异化。" if pct < 0 else "可能释放升级信号，适合跟进对比内容。")
                ),
                "severity": "high" if abs(pct) >= 0.15 else "medium",
                "confidence": 0.9,
                "evidence_json": [{
                    "type": "pricing_change", "competitor": entity,
                    "old_price": old_p, "new_price": new_p, "change_pct": round(pct, 4),
                }],
                "actions_json": [
                    {"action_type": "mflow.create_content",
                     "description": f"产出 {entity} 定价变化对比分析"},
                    {"action_type": "openflow.webhook_insight",
                     "description": "推送洞察到 OpenFlow CDP"},
                ],
                "stage_tags_json": ["S3", "pricing", "competitor"],
            })
        return insights


class NPSModel(InsightModel):
    """NPS 模型（分数异动 + 低分人群圈出）"""

    @property
    def id(self) -> str:
        return "nps"

    @property
    def name(self) -> str:
        return "NPS 健康"

    @property
    def description(self) -> str:
        return "NPS 分数异动监测与贬损者干预建议"

    @property
    def required_metrics(self) -> list[str]:
        return ["nps_score"]

    async def evaluate(self, ctx: ModelContext) -> list[dict]:
        insights = []
        series = sorted([m for m in ctx.metrics if m.metric == "nps_score"], key=lambda m: m.ts)
        if not series:
            return insights
        latest = series[-1]
        score = latest.value

        prev = series[-2].value if len(series) >= 2 else None
        if prev is not None and abs(score - prev) >= 10:
            direction = "上升" if score > prev else "下降"
            insights.append({
                "type": "nps_shift",
                "title": f"NPS 大幅{direction}（{prev:+.0f} → {score:+.0f}）",
                "summary": (
                    f"NPS 出现 ≥10 分异动，{direction}可能与近期产品/服务变更相关，"
                    "建议交叉核对版本发布与舆情时间线。"
                ),
                "severity": "high" if score < prev else "medium",
                "confidence": 0.75,
                "evidence_json": [{"type": "nps_series", "prev": prev, "current": score}],
                "actions_json": [
                    {"action_type": "investigate",
                     "description": "抽取本批 NPS 明细，圈出贬损者原因分布"},
                    {"action_type": "openflow.automation",
                     "description": "贬损者 48 小时内自动进入回访旅程"},
                ],
                "stage_tags_json": ["S2", "voice_of_customer"],
            })
        elif score < 0:
            insights.append({
                "type": "nps_negative",
                "title": f"NPS 为负（{score:+.0f}）",
                "summary": "NPS 低于 0 说明贬损者多于推荐者，口碑将拖慢自然增长。",
                "severity": "high",
                "confidence": 0.9,
                "evidence_json": [{"type": "nps_value", "score": score}],
                "actions_json": [
                    {"action_type": "investigate",
                     "description": "立即开展根因访谈与票选修复优先级"},
                ],
                "stage_tags_json": ["S2", "voice_of_customer"],
            })
        return insights


class JourneyGapModel(InsightModel):
    """旅程缺口模型（漏斗相邻步流失异常）"""

    @property
    def id(self) -> str:
        return "journey_gap"

    @property
    def name(self) -> str:
        return "旅程缺口"

    @property
    def description(self) -> str:
        return "检测客户旅程相邻步骤的异常流失断点"

    @property
    def required_metrics(self) -> list[str]:
        return ["journey_step"]

    async def evaluate(self, ctx: ModelContext) -> list[dict]:
        insights = []
        by_entity: dict[str, list] = {}
        for m in ctx.metrics:
            if m.metric != "journey_step":
                continue
            by_entity.setdefault(m.entity_id, []).append(m)

        for _entity, steps in by_entity.items():
            ordered = sorted(steps, key=lambda m: m.ts)
            for prev, cur in zip(ordered, ordered[1:], strict=False):
                prev_dim = prev.dim_json if isinstance(prev.dim_json, dict) else {}
                cur_dim = cur.dim_json if isinstance(cur.dim_json, dict) else {}
                prev_name = prev_dim.get("step_name", "步骤A")
                cur_name = cur_dim.get("step_name", "步骤B")
                prev_step = prev_dim.get("step", prev.value)
                cur_step = cur_dim.get("step", cur.value)
                if prev_step and cur_step and prev_step > 0:
                    conversion = cur_step / prev_step
                    if conversion < 0.35:  # 相邻步转化 <35%（>65% 流失）
                        insights.append({
                            "type": "journey_gap",
                            "title": f"旅程断点：{prev_name} → {cur_name}",
                            "summary": (
                                f"「{prev_name}」→「{cur_name}」转化率仅 {conversion:.0%}"
                                f"（{prev_step:.0f} → {cur_step:.0f}），存在显著流失断点。"
                            ),
                            "severity": "high" if conversion < 0.4 else "medium",
                            "confidence": 0.8,
                            "evidence_json": [{
                                "type": "funnel_drop",
                                "from_step": prev_name, "to_step": cur_name,
                                "before": prev_step, "after": cur_step,
                                "conversion": round(conversion, 4),
                            }],
                            "actions_json": [
                                {"action_type": "investigate",
                                 "description": f"检查 {cur_name} 步骤的加载/表单/引导体验"},
                                {"action_type": "mflow.create_content",
                                 "description": "针对断点前一步的疑虑产澄清内容"},
                            ],
                            "stage_tags_json": ["S2", "journey"],
                        })
        return insights
