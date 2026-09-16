"""Insight Flow 数据成熟度评估模块

DM-1: 成熟度评估问卷（5 维度映射 L0–L4）
DM-4: 阶段判定引擎（综合成熟度/流量/转化/用户确认 → S0–S3）
DM-3: 《数据成熟度报告》生成（雷达图数据 + 五级定位 + Top5 补课清单）
"""

from datetime import UTC, datetime

from ..core.entities import MaturityLevel, WorkspaceStage
from ..core.files import EventBus, ReportStore
from ..core.store import get_store

# ========== DM-1: 成熟度评估问卷定义（5 维度 × 6 题，共 30 题）==========
# 每题 0–3 分：0=无，1=基础，2=进阶，3=成熟

QUESTIONNAIRE = {
    "instrumentation": {
        "name": "埋点能力",
        "questions": [
            {"id": "inst_1", "text": "是否部署了网站分析工具（GA4 等）？", "weights": 1},
            {"id": "inst_2", "text": "关键转化事件是否有明确埋点规范（命名/参数）？"},
            {"id": "inst_3", "text": "服务端埋点或 CDP 行为流是否可用？"},
            {"id": "inst_4", "text": "埋点变更是否有流程管理（tag plan/审查）？"},
            {"id": "inst_5", "text": "数据质量是否有自动校验（事件数/空值监控）？"},
            {"id": "inst_6", "text": "首方 ID 与用户身份打通（登录态去重）程度？"},
        ],
    },
    "toolstack": {
        "name": "工具栈",
        "questions": [
            {"id": "tool_1", "text": "是否使用 Search Console 且有账号管理权？"},
            {"id": "tool_2", "text": "是否有 SEO/关键词工具（Semrush/Ahrefs/DataForSEO 等）？"},
            {"id": "tool_3", "text": "是否有舆情/社媒监测能力？"},
            {"id": "tool_4", "text": "是否有竞品情报工具（流量/广告/定价）？"},
            {"id": "tool_5", "text": "营销自动化/画布（邮件/推送/ journeys）使用程度？"},
            {"id": "tool_6", "text": "BI 看板是否覆盖增长指标（非仅技术团队）？"},
        ],
    },
    "governance": {
        "name": "数据治理",
        "questions": [
            {"id": "gov_1", "text": "是否有统一的指标字典（口径一致）？"},
            {"id": "gov_2", "text": "数据访问权限与隐私合规（GDPR/个保法）是否落实？"},
            {"id": "gov_3", "text": "数据管道是否有 owner 与 SLA？"},
            {"id": "gov_4", "text": "历史数据是否可回溯（快照/版本化）？"},
            {"id": "gov_5", "text": "跨部门数据协作是否有固定机制？"},
            {"id": "gov_6", "text": "数据字典/血缘文档覆盖核心数据流程度？"},
        ],
    },
    "attribution": {
        "name": "归因能力",
        "questions": [
            {"id": "attr_1", "text": "是否建立了 UTM 规范并强制执行？"},
            {"id": "attr_2", "text": "是否有渠道级转化归因（首次/末次点击以上）？"},
            {"id": "attr_3", "text": "是否有 MMM 或增量实验（geo lift/holdout）能力？"},
            {"id": "attr_4", "text": "内容/SEO 贡献是否可量化到收入？"},
            {"id": "attr_5", "text": "付费与自然渠道是否区分核算？"},
            {"id": "attr_6", "text": "AI-Search（AI Overview 等）引流是否有单独追踪？"},
        ],
    },
    "automation": {
        "name": "自动化程度",
        "questions": [
            {"id": "auto_1", "text": "报表是否自动产出（非手工每周拼表）？"},
            {"id": "auto_2", "text": "异常是否自动告警（流量/转化）？"},
            {"id": "auto_3", "text": "洞察到动作的链路是否系统化（非人肉搬运）？"},
            {"id": "auto_4", "text": "内容/投放是否与数据系统联动（自动触发）？"},
            {"id": "auto_5", "text": "个性化/分群运营是否自动化运行？"},
            {"id": "auto_6", "text": "效果验证（A/B 或前后对比）是否流程化？"},
        ],
    },
}

# 维度 → 补课清单模板（Top5 行动建议来源）
GAP_PLAYBOOK = {
    "instrumentation": [
        "部署 GA4 并埋点核心转化事件（关联：第一方接入向导）",
        "建立埋点规范文档与变更审查流程（关联：技术健康扫描）",
    ],
    "toolstack": [
        "接入 GSC/GA4/CrUX 免费数据底座（关联：第一方接入向导）",
        "补齐竞品/SEO 情报工具（关联：竞品情报模块）",
    ],
    "governance": [
        "建立指标字典统一口径（关联：诊断报告）",
        "落实隐私合规与数据权限（关联：合规基线）",
    ],
    "attribution": [
        "强制 UTM 规范并落渠道归因（关联：流量诊断台）",
        "为内容/SEO 建立贡献度量（关联：动作验证状态机）",
    ],
    "automation": [
        "开启自动告警（流量/转化波动）（关联：告警出站）",
        "打通洞察→执行自动化（关联：OpenFlow/MFlow 集成）",
    ],
}


class MaturityEngine:
    """成熟度评估 + 阶段判定引擎"""

    def __init__(self, workspace_id: str):
        self.workspace_id = workspace_id
        self.bus = EventBus(workspace_id)

    # ========== DM-1: 问卷评分 ==========

    def score(self, answers: dict[str, int]) -> dict:
        """计算问卷得分

        answers: {"inst_1": 2, "tool_3": 1, ...}，每题 0–3
        返回：五维雷达 + 总分 + L0–L4 等级
        """
        radar = {}
        total = 0
        max_total = 0

        for dim_key, dim in QUESTIONNAIRE.items():
            dim_scores = [answers.get(q["id"], 0) for q in dim["questions"]]
            dim_max = len(dim_scores) * 3
            dim_total = sum(dim_scores)
            radar[dim_key] = {
                "name": dim["name"],
                "score": dim_total,
                "max": dim_max,
                "pct": round(dim_total / dim_max, 3) if dim_max else 0.0,
            }
            total += dim_total
            max_total += dim_max

        overall_pct = total / max_total if max_total else 0.0
        level = self._pct_to_level(overall_pct)

        return {
            "answers": answers,
            "radar": radar,
            "total": total,
            "max": max_total,
            "overall_pct": round(overall_pct, 3),
            "level": level.value,
        }

    def _pct_to_level(self, pct: float) -> MaturityLevel:
        """百分比 → L0–L4（参考 BCG 数字营销成熟度模型）"""
        if pct < 0.15:
            return MaturityLevel.L0
        elif pct < 0.35:
            return MaturityLevel.L1
        elif pct < 0.60:
            return MaturityLevel.L2
        elif pct < 0.85:
            return MaturityLevel.L3
        return MaturityLevel.L4

    # ========== DM-4: 阶段判定引擎 ==========

    def determine_stage(
        self,
        maturity_pct: float,
        monthly_sessions: float = 0,
        conversion_rate: float = 0.0,
        user_confirmed_stage: str | None = None,
        months_since_launch: int | None = None,
    ) -> dict:
        """综合判定增长阶段 S0–S3

        规则（成熟度评分 + 流量规模 + 转化数据 + 用户确认）：
        - 用户确认优先（但需满足最低数据条件，否则给 warning）
        - months_since_launch < 6 或 PMF 未验证 → S0
        - 会话 < 1000/月 → S0/S1 分界看转化
        - 有稳定用户盘（>10k/月）→ S2
        - 收入模型待优化（转化 > 2% 且规模大）→ S3
        阶段可并存：返回 primary + allowed
        """
        signals = {
            "maturity_pct": round(maturity_pct, 3),
            "monthly_sessions": monthly_sessions,
            "conversion_rate": conversion_rate,
            "months_since_launch": months_since_launch,
            "user_confirmed_stage": user_confirmed_stage,
        }

        # 引擎判定
        if months_since_launch is not None and months_since_launch < 6:
            engine_stage = "S0"
        elif monthly_sessions < 1000:
            engine_stage = "S0" if conversion_rate < 0.01 else "S1"
        elif monthly_sessions < 10000:
            engine_stage = "S1"
        elif conversion_rate < 0.02:
            engine_stage = "S2"
        else:
            engine_stage = "S3"

        # 用户确认优先，不一致时降级为 warning
        final_stage = engine_stage
        conflict = None
        if user_confirmed_stage and user_confirmed_stage != engine_stage:
            final_stage = user_confirmed_stage
            conflict = f"用户自报 {user_confirmed_stage} 与引擎判定 {engine_stage} 不一致，以用户确认为准"

        return {
            "stage": final_stage,
            "engine_stage": engine_stage,
            "signals": signals,
            "conflict": conflict,
        }

    # ========== DM-3: 成熟度报告 ==========

    def build_report(self, score_result: dict, stage_result: dict) -> str:
        """生成《数据成熟度报告》Markdown（雷达数据 + 五级定位 + Top5 补课清单）"""
        now = datetime.now(UTC)
        radar = score_result["radar"]
        level = score_result["level"]

        # 找出最弱的两个维度 + Top5 补课清单
        ranked = sorted(radar.items(), key=lambda kv: kv[1]["pct"])
        weakest = [k for k, _ in ranked[:2]]
        gaps = []
        for dim in weakest:
            for item in GAP_PLAYBOOK.get(dim, []):
                gaps.append({"dimension": radar[dim]["name"], "action": item})
        top5 = gaps[:5]

        lines = [
            "# 数据成熟度报告",
            "",
            "| 项 | 内容 |",
            "|---|---|",
            f"| 工作区 | {self.workspace_id} |",
            f"| 生成时间 | {now.strftime('%Y-%m-%d %H:%M UTC')} |",
            f"| 总体得分 | {score_result['total']}/{score_result['max']}（{score_result['overall_pct']:.0%}） |",
            f"| 成熟度等级 | **{level}**（L0 无数据 / L1 基础 / L2 进阶 / L3 完整 / L4 智能） |",
            f"| 增长阶段 | **{stage_result['stage']}**（引擎判定 {stage_result['engine_stage']}） |",
            "",
            "## 五维雷达",
            "",
            "| 维度 | 得分 | 满分 | 百分比 |",
            "|---|---|---|---|",
        ]
        for dim_key, dim in radar.items():
            bar = "█" * int(dim["pct"] * 10) + "░" * (10 - int(dim["pct"] * 10))
            lines.append(f"| {dim['name']} | {dim['score']} | {dim['max']} | {dim['pct']:.0%} {bar} |")

        lines += [
            "",
            "## Top5 补课清单（每项关联具体模块）",
            "",
        ]
        for i, gap in enumerate(top5, 1):
            lines.append(f"{i}. 【{gap['dimension']}】{gap['action']}")

        if stage_result.get("conflict"):
            lines += ["", f"> ⚠️ {stage_result['conflict']}"]

        return "\n".join(lines)

    # ========== 一键评估流程 ==========

    async def assess(
        self,
        answers: dict[str, int],
        monthly_sessions: float = 0,
        conversion_rate: float = 0.0,
        user_confirmed_stage: str | None = None,
        months_since_launch: int | None = None,
    ) -> dict:
        """执行完整评估：评分 → 阶段判定 → 写库 → 报告落盘"""
        self.bus.emit("maturity.assessment_started", {})

        score_result = self.score(answers)
        stage_result = self.determine_stage(
            score_result["overall_pct"],
            monthly_sessions=monthly_sessions,
            conversion_rate=conversion_rate,
            user_confirmed_stage=user_confirmed_stage,
            months_since_launch=months_since_launch,
        )

        # 写回 workspace（stage + maturity_level 由成熟度引擎写入）
        store = await get_store()
        ws = await store.get_workspace(self.workspace_id)
        if ws:
            ws.stage = WorkspaceStage(stage_result["stage"])
            ws.maturity_level = MaturityLevel(score_result["level"])
            await store.update_workspace(ws)

        # 报告落盘
        report_md = self.build_report(score_result, stage_result)
        report_path = ReportStore(self.workspace_id).save_report("maturity", report_md)

        self.bus.emit("report.ready", {"kind": "maturity", "path": str(report_path)})

        return {
            "score": score_result,
            "stage": stage_result,
            "report_path": str(report_path),
        }
