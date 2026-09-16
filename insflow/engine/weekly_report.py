"""Insight Flow 周报体系

聚合三类报告（竞品动向 / 流量诊断 / 舆情·洞察）+ 动作验证结果
→ 管理层增长简报（对应 Skill: weekly-brief-writer）
→ 自动落 MFlow `1-2 Insight/Insight Flow Reports/`（MFlow 报告中心可见）
"""

import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..core.files import EventBus, ReportStore
from ..core.store import get_store


class WeeklyReportBuilder:
    """周报生成器（无人值守，可由调度器 cron 触发）"""

    def __init__(self, workspace_id: str):
        self.workspace_id = workspace_id
        self.bus = EventBus(workspace_id)

    async def build(self) -> dict:
        """生成周报（数据全部来自工具查询，不编造数字）"""
        store = await get_store()
        now = datetime.now(UTC)
        week_ago = now - timedelta(days=7)

        insights = await store.list_insights(self.workspace_id, limit=200)
        week_insights = [i for i in insights if i.created_at >= week_ago]
        week_insights.sort(
            key=lambda i: ({"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}[i.severity.value], i.created_at),
            reverse=True,
        )
        feedback = await store.get_feedback_stats(self.workspace_id)
        reports = ReportStore(self.workspace_id)
        refs = {cat: [p.name for p in reports.list_reports(cat)]
                for cat in ("diagnosis", "competitors", "maturity", "verification")}

        # 报告即文件
        md = self._render(now, week_ago, week_insights, feedback, refs)
        path = reports.save_report("weekly", md)
        self.bus.emit("report.ready", {"kind": "weekly", "path": str(path)})

        # 同步落 MFlow 报告目录（1-2 Insight/Insight Flow Reports/）
        mflow_path = self.sync_to_mflow(path)
        return {
            "report_path": str(path),
            "mflow_path": str(mflow_path) if mflow_path else None,
            "insights_count": len(week_insights),
            "feedback": feedback,
        }

    def _render(self, now: datetime, week_ago: datetime, insights: list,
                feedback: dict, refs: dict) -> str:
        effective = feedback.get("effective", 0)
        neutral = feedback.get("neutral", 0)
        harmful = feedback.get("harmful", 0)

        lines = [
            "# Insight Flow 增长周报",
            "",
            "| 项 | 内容 |",
            "|---|---|",
            f"| 工作区 | {self.workspace_id} |",
            f"| 周期 | {week_ago.strftime('%Y-%m-%d')} ~ {now.strftime('%Y-%m-%d')} |",
            f"| 本周洞察 | {len(insights)} 条 |",
            f"| 动作验证 | ✅{effective} ➖{neutral} ❌{harmful} |",
            "",
            "## 本周摘要",
            "",
        ]
        if insights:
            top = insights[0]
            lines.append(
                f"最重要的信号：**{top.title}**（{top.severity.value} 级，"
                f"置信度 {top.confidence:.0%}）。"
            )
        else:
            lines.append("本周无新增洞察。")

        lines += ["", "## 洞察 Top5", ""]
        for i, ins in enumerate(insights[:5], 1):
            lines.append(
                f"{i}. [{ins.severity.value.upper()}] {ins.title}"
                f" [ins:{ins.id}]"
            )

        lines += ["", "## 本周引用的报告", ""]
        for cat, files in refs.items():
            if files:
                lines.append(f"- {cat}: {', '.join(files[:3])}")

        lines += ["", "## 数据来源", ""]
        for ins in insights[:10]:
            lines.append(f"- ins:{ins.id}（{ins.created_at.strftime('%m-%d %H:%M')}）")

        return "\n".join(lines)

    # ========== MFlow 报告同步（独立子目录，不与现有 SEO 报告混目录）==========

    def sync_to_mflow(self, report_path: Path) -> Path | None:
        """复制到 MFlow `1-2 Insight/Insight Flow Reports/`

        经环境变量 LOVART_LOCAL_DEV_ROOT 定位 MFlow 数据落点（运行时推导）。
        MFlow 报告中心（console REPORT_CATS）会自动收录该目录。
        """
        root = os.environ.get("LOVART_LOCAL_DEV_ROOT", "")
        if not root or not report_path.exists():
            return None

        target_dir = Path(root) / "1-2 Insight" / "Insight Flow Reports" / Path(report_path).parent.name
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / report_path.name
        shutil.copy2(report_path, target)
        self.bus.emit("mflow.report_synced", {"path": str(target)})
        return target
