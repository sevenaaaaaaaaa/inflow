"""Insight Flow 深度报告生成器（多 Agent 协作，顾问场景交付物）

管线（任务状态机 queued → running → done | failed）：
1. Collector Agent：拉取全量数据（洞察/验证效果/成熟度/旅程/竞品档案）
2. 三位分析 Agent 并行消费：流量分析师 / 竞品分析师 / 客户旅程分析师
   （各自由对应 Skill 方法论驱动）
3. Editor Agent：合并为最终交付物（执行摘要/分领域发现/Top5 行动/90 天路线图）
4. 报告落盘 reports/deep-dive/ + 事件流
"""

from datetime import UTC, datetime

from ..core.files import EventBus, ReportStore
from ..core.statemachine import TASK_MACHINE
from ..core.store import get_store

SECTION_NAMES = {
    "traffic-analyst": "流量诊断",
    "competitor-analyst": "竞争格局",
    "journey-analyst": "客户旅程与分层",
}


class DeepReportBuilder:
    """深度报告生成器（多 Agent 协作管线）"""

    def __init__(self, workspace_id: str):
        self.workspace_id = workspace_id
        self.bus = EventBus(workspace_id)

    # ========== Collector Agent ==========

    async def collect_context(self) -> dict:
        """数据采集：一次拉齐所有分析 Agent 需要的输入"""
        store = await get_store()
        insights = await store.list_insights(self.workspace_id, limit=200)
        feedback = await store.get_feedback_stats(self.workspace_id)
        effectiveness = await store.get_model_effectiveness(self.workspace_id)
        competitors = await store.list_competitors(self.workspace_id)
        journey_events = await store.list_journey_events(self.workspace_id, limit=200)
        ws = await store.get_workspace(self.workspace_id)

        return {
            "workspace": {
                "id": self.workspace_id,
                "stage": ws.stage.value if ws else "S0",
                "maturity_level": ws.maturity_level.value if ws else "L0",
            },
            "insights": [{
                "id": i.id, "type": i.type, "title": i.title,
                "severity": i.severity.value, "confidence": i.confidence,
                "summary": i.summary, "actions": [a.model_dump() for a in i.actions_json],
            } for i in insights],
            "feedback": feedback,
            "model_effectiveness": effectiveness,
            "competitors": competitors,
            "journey_events_count": len(journey_events),
        }

    # ========== 分析 Agents（Skill 方法论驱动）==========

    async def _traffic_analyst(self, data: dict) -> dict:
        """流量分析师（方法论: traffic-attribution-diagnosis）"""
        signals = [i for i in data["insights"]
                   if i["type"] in ("traffic_anomaly", "conversion_low", "keyword_opportunity")]
        conclusion = (
            f"流量面共 {len(signals)} 条信号；"
            + (f"首要问题：{signals[0]['title']}。" if signals else "近期无流量异常。")
        )
        return {"agent": "traffic-analyst", "skill": "traffic-attribution-diagnosis",
                "signals": signals, "conclusion": conclusion}

    async def _competitor_analyst(self, data: dict) -> dict:
        """竞品分析师（方法论: competitor-move-analysis）"""
        signals = [i for i in data["insights"]
                   if i["type"] in ("competitor_pricing", "site_change", "keyword_gap")]
        profiles = data["competitors"]
        conclusion = (
            f"追踪 {len(profiles)} 个竞品档案，报告期内 {len(signals)} 条异动信号；"
            + (f"最值得注意：{signals[0]['title']}。" if signals else "竞争面平稳。")
        )
        return {"agent": "competitor-analyst", "skill": "competitor-move-analysis",
                "signals": signals,
                "profiles": [p.get("name", p.get("domain", "")) for p in profiles],
                "conclusion": conclusion}

    async def _journey_analyst(self, data: dict) -> dict:
        """旅程分析师（方法论: journey-gap-analysis）"""
        signals = [i for i in data["insights"]
                   if i["type"] in ("journey_content_gap", "rfm_at_risk", "journey_gap")]
        return {"agent": "journey-analyst", "skill": "journey-gap-analysis",
                "signals": signals,
                "events_tracked": data["journey_events_count"],
                "conclusion": (
                    f"旅程/分层共 {len(signals)} 条断点与分群信号"
                    f"（事件数据 {data['journey_events_count']} 条）。"
                )}

    # ========== Editor Agent ==========

    def _editor(self, data: dict, analyses: list[dict]) -> str:
        """合并为顾问交付物"""
        now = datetime.now(UTC)
        stage = data["workspace"]["stage"]
        level = data["workspace"]["maturity_level"]
        feedback = data["feedback"]
        eff, neu, harm = (feedback.get("effective", 0),
                          feedback.get("neutral", 0),
                          feedback.get("harmful", 0))

        top_actions: list[dict] = []
        for ins in data["insights"][:10]:
            for a in ins.get("actions", []):
                if isinstance(a, dict) and a.get("description"):
                    top_actions.append({
                        "title": a["description"][:60],
                        "type": a.get("action_type", "investigate"),
                        "insight": ins["id"],
                    })
        top5 = top_actions[:5]

        lines = [
            "# Insight Flow 深度诊断报告",
            "",
            "| 项 | 内容 |",
            "|---|---|",
            f"| 工作区 | {self.workspace_id} |",
            f"| 生成时间 | {now.strftime('%Y-%m-%d %H:%M UTC')} |",
            f"| 增长阶段 | {stage} |",
            f"| 数据成熟度 | {level} |",
            f"| 分析 Agents | {', '.join(a['agent'] for a in analyses)} |",
            "",
            "## 执行摘要",
            "",
        ]
        for a in analyses:
            lines.append(f"- **{a['agent']}**：{a['conclusion']}")

        lines += ["", "## 分领域发现", ""]
        for a in analyses:
            lines.append(f"### {SECTION_NAMES.get(a['agent'], a['agent'])}")
            lines.append("")
            lines.append(a["conclusion"])
            for s in a.get("signals", [])[:5]:
                lines.append(f"- {s['title']} [ins:{s['id']}]")
            lines.append("")

        lines += ["", "## Top5 行动建议（每条带洞察溯源）", ""]
        for i, act in enumerate(top5, 1):
            lines.append(f"{i}. {act['title']}（{act['type']}，溯源 ins:{act['insight']}）")

        lines += ["", "## 90 天路线图", ""]
        for title, desc in self._roadmap():
            lines.append(f"- **{title}**：{desc}")

        lines += [
            "",
            "## 验证口径",
            "",
            f"历史动作验证：✅ 有效 {eff} · ➖ 中性 {neu} · ❌ 有害 {harm}",
            "",
        ]
        if data["model_effectiveness"]:
            lines += ["| 模型 | 命中率 | 有效/中性/有害 |", "|---|---|---|"]
            for m in data["model_effectiveness"][:5]:
                lines.append(
                    f"| {m['model_id']} | {m['hit_rate']:.0%} | "
                    f"{m['effective']}/{m['neutral']}/{m['harmful']} |"
                )
            lines.append("")

        return "\n".join(lines)

    def _roadmap(self) -> list[tuple[str, str]]:
        return [
            ("0-30 天（止血）", "修复已验证 harmful 的动作链路；处理 critical/high 洞察"),
            ("31-60 天（补课）", "覆盖旅程空心阶段内容；落地关键词缺口 Top5"),
            ("61-90 天（放大）", "按模型命中率加码 effective 类洞察的执行；建立月度复盘"),
        ]

    # ========== 主入口 ==========

    async def build(self) -> dict:
        """生成深度报告"""
        self.bus.emit("task.started", {"kind": "deep-dive"})
        TASK_MACHINE.validate("queued", "running")

        data = await self.collect_context()
        analyses = [
            await self._traffic_analyst(data),
            await self._competitor_analyst(data),
            await self._journey_analyst(data),
        ]
        md = self._editor(data, analyses)

        path = ReportStore(self.workspace_id).save_report("deep-dive", md)
        TASK_MACHINE.validate("running", "done")
        self.bus.emit("report.ready", {"kind": "deep-dive", "path": str(path)})
        return {"report_path": str(path), "analysts": [a["agent"] for a in analyses]}
