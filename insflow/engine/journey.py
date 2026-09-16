"""Insight Flow 客户旅程模块

CJ-1 旅程框架内置：See-Think-Do-Care（默认）/ AIDA / 自定义五段
CJ-2 触点映射：内容/广告/竞品内容 → 旅程阶段 → 覆盖热力图（哪个阶段空心）
CJ-4 旅程断点清单：直接生成内容缺口清单（→ MFlow）
"""


from ..core.entities import Insight
from ..core.files import EventBus
from ..core.store import get_store

# ========== CJ-1 旅程框架 ==========

JOURNEY_FRAMEWORKS: dict[str, dict] = {
    "see-think-do-care": {
        "name": "See-Think-Do-Care",
        "stages": [
            {
                "id": "see",
                "name": "See（看见）",
                "definition": "潜在用户尚未意识到明确需求",
                "entry_signals": ["广泛兴趣内容曝光", "社媒触达"],
                "metrics": ["曝光量", "触达人数"],
                "content_types": ["科普", "趋势", "泛兴趣内容"],
            },
            {
                "id": "think",
                "name": "Think（考虑）",
                "definition": "用户开始搜索与比较方案",
                "entry_signals": ["搜索品类词", "访问对比页"],
                "metrics": ["搜索排名", "对比页流量", "停留时长"],
                "content_types": ["评测", "对比", "教程", "案例"],
            },
            {
                "id": "do",
                "name": "Do（行动）",
                "definition": "用户决策购买",
                "entry_signals": ["搜索品牌词", "访问定价页", "加购"],
                "metrics": ["定价页转化", "试用注册", "成交"],
                "content_types": ["定价页", "FAQ", "保障说明", "CTA"],
            },
            {
                "id": "care",
                "name": "Care（关怀）",
                "definition": "已购用户的留存与增值",
                "entry_signals": ["注册/购买完成", "活跃使用"],
                "metrics": ["留存率", "复购", "NPS", "推荐"],
                "content_types": ["进阶教程", "社区", "升级路径"],
            },
        ],
    },
    "aida": {
        "name": "AIDA",
        "stages": [
            {"id": "awareness", "name": "注意（Awareness）", "definition": "首次接触",
             "entry_signals": ["首次访问"], "metrics": ["新访客"],
             "content_types": ["品牌内容"]},
            {"id": "interest", "name": "兴趣（Interest）", "definition": "产生兴趣",
             "entry_signals": ["多页浏览"], "metrics": ["页面深度"],
             "content_types": ["教育内容"]},
            {"id": "desire", "name": "欲望（Desire）", "definition": "形成购买意愿",
             "entry_signals": ["访问定价/案例"], "metrics": ["定价页流量"],
             "content_types": ["案例/证言"]},
            {"id": "action", "name": "行动（Action）", "definition": "完成转化",
             "entry_signals": ["注册/购买"], "metrics": ["转化率"],
             "content_types": ["转化页"]},
        ],
    },
}


def get_framework(name: str = "see-think-do-care") -> dict:
    framework = JOURNEY_FRAMEWORKS.get(name)
    if not framework:
        raise ValueError(f"未知旅程框架: {name}（可用: {', '.join(JOURNEY_FRAMEWORKS)}）")
    return framework


class JourneyModule:
    """客户旅程模块"""

    def __init__(self, workspace_id: str):
        self.workspace_id = workspace_id
        self.bus = EventBus(workspace_id)

    # ========== CJ-2 触点映射 + 覆盖热力图 ==========

    def map_touchpoints(self, touchpoints: list[dict],
                        framework: str = "see-think-do-care") -> dict:
        """把触点（内容/广告/竞品内容）映射到旅程阶段 → 覆盖热力图

        touchpoints: [{"kind": "article", "title": "...", "stage_hint": "think"}, ...]
        stage_hint 可显式指定，或按关键词自动推断（content_types 匹配）
        """
        fw = get_framework(framework)
        coverage = {s["id"]: {"name": s["name"], "count": 0, "items": []} for s in fw["stages"]}

        for tp in touchpoints:
            stage = tp.get("stage_hint") or self._infer_stage(tp, fw)
            if stage and stage in coverage:
                coverage[stage]["count"] += 1
                coverage[stage]["items"].append(tp.get("title", "")[:50])

        # 空心阶段：触点数 < 阈值
        hollow = [sid for sid, data in coverage.items() if data["count"] < 3]

        return {
            "framework": framework,
            "coverage": coverage,
            "heatmap": {sid: min(1.0, d["count"] / 10) for sid, d in coverage.items()},
            "hollow_stages": hollow,
            "total_touchpoints": len(touchpoints),
        }

    def _infer_stage(self, touchpoint: dict, framework: dict) -> str | None:
        """按内容类型关键词推断阶段"""
        text = (touchpoint.get("title", "") + touchpoint.get("kind", "")).lower()
        for stage in framework["stages"]:
            for ct in stage["content_types"]:
                if ct.lower() in text:
                    return stage["id"]
        # 默认 See（无法推断时归入顶层漏斗）
        return framework["stages"][0]["id"]

    # ========== CJ-4 断点清单（内容缺口 → MFlow）==========

    async def build_gap_insights(self, heatmap_result: dict) -> list[Insight]:
        """空心阶段 → 内容缺口洞察（断点清单，直接映射 MFlow 选题）"""
        store = await get_store()
        fw = get_framework("see-think-do-care")
        stage_map = {s["id"]: s for s in fw["stages"]}

        insights: list[Insight] = []
        for stage_id in heatmap_result.get("hollow_stages", []):
            stage = stage_map.get(stage_id)
            if not stage:
                continue

            insight = Insight(
                workspace_id=self.workspace_id,
                type="journey_content_gap",
                title=f"旅程空心：{stage['name']} 阶段内容覆盖不足",
                summary=(
                    f"「{stage['name']}」阶段现有触点不足 3 个，该阶段衡量指标为 "
                    f"{'/'.join(stage['metrics'])}。补课方向：{', '.join(stage['content_types'])}。"
                ),
                severity="medium",
                confidence=0.75,
                evidence_json=[{
                    "type": "coverage_heatmap",
                    "stage": stage_id,
                    "stage_name": stage["name"],
                    "expected_content_types": stage["content_types"],
                    "entry_signals": stage["entry_signals"],
                }],
                models_json=["journey_coverage"],
                actions_json=[
                    {
                        "action_type": "mflow.create_content",
                        "description": f"为 {stage['name']} 阶段补内容：{', '.join(stage['content_types'][:3])}",
                    },
                    {
                        "action_type": "mflow.register_topic",
                        "description": "把缺口选题登记进 MFlow 状态机",
                    },
                ],
                stage_tags_json=["journey", "content"],
            )

            from ..engine.quality_gates import get_quality_gates
            if get_quality_gates().validate(insight).passed:
                saved = await store.create_insight(insight)
                self.bus.emit("insight.created", {
                    "insight_id": saved.id, "type": saved.type,
                })
                insights.append(saved)

        return insights
