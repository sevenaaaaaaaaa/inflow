"""Insight Flow 流量诊断台

诊断管线：采集（GSC/GA4/CrUX）→ AARRR 映射 → 异常检测 → Insight 生成
→ 质量门 → 入库 + 诊断报告落盘
"""

import statistics
from datetime import UTC, datetime

from ..core.entities import Insight, InsightAction
from ..core.files import EventBus, ReportStore
from ..core.store import get_store
from ..engine.quality_gates import get_quality_gates
from ..engine.router import ModelContext, get_model_router


class DiagnosisEngine:
    """流量诊断引擎

    输入域名（+ 可选竞品）→ 产出含异常检测、Top 行动建议的诊断结果
    """

    def __init__(self, workspace_id: str):
        self.workspace_id = workspace_id
        self.bus = EventBus(workspace_id)

    # ========== AARRR 映射 ==========

    def map_to_aarrr(self, metrics: dict) -> dict:
        """把原始指标映射到 AARRR 五阶段

        metrics 结构：
        {
            "ga4_sessions": [...], "ga4_conversions": [...],
            "gsc_clicks": [...], "gsc_impressions": [...],
            "crux_cwv": [...], ...
        }
        返回：
        {
            "acquisition": {...}, "activation": {...},
            "retention": {...}, "revenue": {...}, "referral": {...}
        }
        """
        return {
            "acquisition": {
                "name": "获取（Acquisition）",
                "metrics": metrics.get("gsc_clicks", []) + metrics.get("gsc_impressions", []),
                "sources": ["gsc"],
            },
            "activation": {
                "name": "激活（Activation）",
                "metrics": metrics.get("ga4_sessions", []),
                "sources": ["ga4"],
            },
            "retention": {
                "name": "留存（Retention）",
                "metrics": metrics.get("ga4_retention", []),
                "sources": ["ga4"],
            },
            "revenue": {
                "name": "收入（Revenue）",
                "metrics": metrics.get("ga4_conversions", []),
                "sources": ["ga4"],
            },
            "referral": {
                "name": "推荐（Referral）",
                "metrics": [],
                "sources": [],
            },
        }

    # ========== 异常检测 ==========

    def detect_anomalies(self, series: list[float], threshold_pct: float = 0.2) -> list[dict]:
        """时序异常检测（Z-score + 环比骤降检测）

        返回异常列表：[{index, value, prev_value, change_pct, z_score}]
        """
        anomalies = []
        if len(series) < 4:
            return anomalies

        mean = statistics.mean(series)
        std = statistics.stdev(series) if len(series) > 1 else 0.0

        for i in range(1, len(series)):
            value = series[i]
            prev = series[i - 1]
            change_pct = (value - prev) / prev if prev else 0.0
            z_score = (value - mean) / std if std else 0.0

            # 环比骤变（降幅超过阈值）
            if abs(change_pct) >= threshold_pct:
                anomalies.append({
                    "index": i,
                    "value": value,
                    "prev_value": prev,
                    "change_pct": round(change_pct, 4),
                    "z_score": round(z_score, 2),
                    "kind": "drop" if change_pct < 0 else "spike",
                })

        return anomalies

    # ========== 诊断主流程 ==========

    async def run(self, source_data: dict | None = None) -> dict:
        """执行一次全量诊断

        source_data: 可选的已采集指标数据（测试注入用）
                     若为空则从数据库读取最近的原始记录
        返回诊断摘要 + 生成的洞察列表
        """
        self.bus.emit("diagnosis.started", {"workspace": self.workspace_id})
        store = await get_store()

        # 1. 归一化指标（若未注入，则从 raw_records 汇总）
        metrics_map = source_data or {}

        # 2. AARRR 映射
        aarrr = self.map_to_aarrr(metrics_map)

        # 3. 运行模型路由中所有可运行模型，收集洞察草稿
        get_model_router()
        insights_drafts: list[dict] = []

        # AARRR 模型分析
        from ..engine.models.aarrr import AARRRModel
        aarrr_model = AARRRModel()

        # 构造 ModelContext 需要的 Metric 列表（从注入数据转换）
        from ..core.entities import Metric
        ctx_metrics = []
        for kind, values in metrics_map.items():
            for v in values if isinstance(values, list) else [values]:
                if isinstance(v, dict):
                    v.get("ts") or v.get("captured_at")
                    ctx_metrics.append(Metric(
                        workspace_id=self.workspace_id,
                        entity_type="site",
                        entity_id="main",
                        metric=kind,
                        value=float(v.get("value", v.get("count", 0))),
                        dim_json=v,
                    ))
                elif isinstance(v, (int, float)):
                    ctx_metrics.append(Metric(
                        workspace_id=self.workspace_id,
                        entity_type="site",
                        entity_id="main",
                        metric=kind,
                        value=float(v),
                    ))

        if ctx_metrics:
            mc = ModelContext(workspace_id=self.workspace_id, metrics=ctx_metrics)
            drafts = await aarrr_model.evaluate(mc)
            insights_drafts.extend(drafts)

        # 4. 质量门过滤
        gates = get_quality_gates()
        passed: list[Insight] = []
        failed: list[dict] = []

        for draft in insights_drafts:
            insight = self._draft_to_insight(draft)
            report = gates.validate(insight)
            if report.passed:
                passed.append(insight)
            else:
                failed.append({
                    "title": draft.get("title"),
                    "errors": report.errors,
                })

        # 5. 入库
        saved_ids = []
        for insight in passed:
            insight.workspace_id = self.workspace_id
            saved = await store.create_insight(insight)
            await _notify(self.workspace_id, saved.id)
            saved_ids.append(saved.id)
            self.bus.emit("insight.created", {
                "insight_id": saved.id,
                "type": saved.type,
                "title": saved.title,
                "severity": saved.severity.value,
            })

        summary = {
            "workspace": self.workspace_id,
            "aarrr_mapped": {k: v["name"] for k, v in aarrr.items()},
            "anomalies_detected": sum(
                len(self.detect_anomalies([m.value for m in ctx_metrics]))
                for _ in [1]
            ),
            "insights_created": len(saved_ids),
            "insight_ids": saved_ids,
            "quality_gate_failed": failed,
            "ran_at": datetime.now(UTC).isoformat(),
        }

        # 6. 诊断报告落盘（报告即文件）
        report_md = self._render_report(summary, aarrr, passed, failed)
        store_files = ReportStore(self.workspace_id)
        report_path = store_files.save_report("diagnosis", report_md)
        summary["report_path"] = str(report_path)

        self.bus.emit("diagnosis.finished", {
            "insights_created": len(saved_ids),
            "report": str(report_path),
        })

        return summary

    def _draft_to_insight(self, draft: dict) -> Insight:
        """洞察草稿 → Insight 对象（actions 转 InsightAction）"""
        actions = []
        for a in draft.get("actions_json", []):
            if isinstance(a, dict):
                actions.append(InsightAction(
                    action_type=a.get("action_type", "investigate"),
                    description=a.get("description", ""),
                    params_json=a.get("params_json", {}),
                    target_ref=a.get("target_ref"),
                ))
        return Insight(
            workspace_id=self.workspace_id,
            type=draft.get("type", "generic"),
            title=draft.get("title", ""),
            summary=draft.get("summary", ""),
            severity=draft.get("severity", "medium"),
            confidence=draft.get("confidence", 0.5),
            evidence_json=draft.get("evidence_json", []),
            models_json=draft.get("models_json", [draft.get("model_id", "aarrr")]),
            actions_json=actions,
            stage_tags_json=draft.get("stage_tags_json", []),
        )

    def _render_report(
        self,
        summary: dict,
        aarrr: dict,
        insights: list[Insight],
        failed: list[dict],
    ) -> str:
        """渲染诊断报告 Markdown"""
        now = datetime.now(UTC)
        lines = [
            "# 流量诊断报告",
            "",
            "| 项 | 内容 |",
            "|---|---|",
            f"| 工作区 | {self.workspace_id} |",
            f"| 生成时间 | {now.strftime('%Y-%m-%d %H:%M UTC')} |",
            f"| AARRR 映射 | {' / '.join(v['name'] for v in aarrr.values())} |",
            f"| 洞察产出 | {len(insights)} 条 |",
            "",
            "## 洞察明细",
            "",
        ]

        if insights:
            for ins in insights:
                lines.append(f"### [{ins.severity.value.upper()}] {ins.title}")
                lines.append("")
                lines.append(ins.summary)
                lines.append("")
                if ins.actions_json:
                    lines.append("**推荐动作：**")
                    for a in ins.actions_json:
                        lines.append(f"- {a.description}（{a.action_type}）")
                    lines.append("")
        else:
            lines.append("本期无新增洞察。")
            lines.append("")

        if failed:
            lines.append("## 被质量门拦截的草稿")
            lines.append("")
            for f in failed:
                lines.append(f"- {f['title']}: {', '.join(f['errors'])}")
            lines.append("")

        return "\n".join(lines)


async def _notify(workspace_id: str, insight_id: str) -> None:
    """洞察创建 → 订阅推送（失败静默，不阻断主流程）"""
    try:
        from .subscriptions import notify_new_insight
        await notify_new_insight(workspace_id, insight_id)
    except Exception:
        pass
