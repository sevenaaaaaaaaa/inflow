"""Insight Flow 竞品情报完整版

CI-1 竞品档案（CompetitorProfile）：域名/定位/定价档位/产品线/社媒矩阵/监测项清单
CI-3 SEO 竞争情报：关键词重叠/缺口、排名追踪（DataForSEO Labs）
CI-7 周报《竞品动向》：全渠道聚合 + "本周最值得注意的 3 件事"
"""

from datetime import datetime, timedelta, timezone

from ..core.entities import Insight
from ..core.files import EventBus, ReportStore
from ..core.store import get_store
from ..engine.change_monitor import ChangeMonitor


class CompetitorModule:
    """竞品档案 + SEO 竞争情报"""

    def __init__(self, workspace_id: str):
        self.workspace_id = workspace_id
        self.bus = EventBus(workspace_id)

    # ========== CI-1 竞品档案 ==========

    async def upsert_profile(self, domain: str, name: str = "", positioning: str = "",
                             pricing: list | None = None, product_lines: list | None = None,
                             social: dict | None = None, monitors: list | None = None) -> dict:
        """创建/更新竞品档案（并自动登记网站变更监控项）"""
        store = await get_store()
        profile = await store.upsert_competitor(self.workspace_id, domain, {
            "name": name or domain,
            "positioning": positioning,
            "pricing": pricing or [],
            "product_lines": product_lines or [],
            "social": social or {},
            "monitors": monitors or ["pricing", "changelog"],
        })
        self.bus.emit("competitor.profile_updated", {"domain": domain})
        return profile

    async def list_profiles(self) -> list[dict]:
        store = await get_store()
        return await store.list_competitors(self.workspace_id)

    # ========== CI-3 SEO 竞争情报 ==========

    async def record_seo_snapshot(self, domain: str, ranks: list[dict]) -> dict:
        """记录一次关键词排名快照 → 生成关键词缺口洞察

        ranks 结构（来自 dataforseo-labs 插件或注入）：
        [{"keyword": "...", "position": 8, "volume": 1200,
          "url": "...", "is_mine": false}, ...]
        """
        store = await get_store()
        saved = await store.save_keyword_ranks(self.workspace_id, domain, ranks)

        mine = [r for r in ranks if r.get("is_mine")]
        theirs = [r for r in ranks if not r.get("is_mine")]
        mine_keywords = {r["keyword"] for r in mine}

        # 关键词缺口：竞品排名 ≤20 且 volume ≥100，我方无排名
        gaps = [r for r in theirs_ranks(theirs=theirs)
                if r["position"] and 0 < r["position"] <= 20
                and r["keyword"] not in mine_keywords
                and r.get("volume", 0) >= 100]

        gap_insight_id = None
        if gaps:
            top = sorted(gaps, key=lambda r: -r.get("volume", 0))[:10]
            insight = await self._keyword_gap_insight(domain, top, len(gaps))
            gap_insight_id = insight.id if insight else None

        self.bus.emit("competitor.seo_snapshot", {
            "domain": domain, "ranks": saved, "gaps": len(gaps),
        })

        return {
            "domain": domain,
            "ranks_recorded": saved,
            "mine": len(mine),
            "theirs": len(theirs),
            "gaps": len(gaps),
            "insight_id": gap_insight_id,
        }

    async def _keyword_gap_insight(self, domain: str, top_gaps: list[dict], total_gaps: int) -> Insight | None:
        store = await get_store()
        kw_list = ", ".join(r["keyword"] for r in top_gaps[:5])
        volumes = sum(r.get("volume", 0) for r in top_gaps)

        insight = Insight(
            workspace_id=self.workspace_id,
            type="keyword_gap",
            title=f"关键词缺口：{domain} 有 {total_gaps} 个词我们没覆盖",
            summary=(
                f"竞品 {domain} 在 {total_gaps} 个关键词上有排名（我们缺失），"
                f"Top10 机会词：{kw_list}，合计月搜索量约 {volumes}。"
                "建议优先覆盖量最大且与产品强相关的 3-5 个词。"
            ),
            severity="medium",
            confidence=0.8,
            evidence_json=[{
                "type": "keyword_gap",
                "competitor": domain,
                "total_gaps": total_gaps,
                "top_keywords": top_gaps,
            }],
            models_json=["keyword_gap_tracker"],
            actions_json=[
                {
                    "action_type": "mflow.create_content",
                    "description": f"围绕缺口词产内容：{kw_list[:60]}",
                },
                {
                    "action_type": "openflow.webhook_insight",
                    "description": "推送关键词缺口到 OpenFlow CDP",
                },
            ],
            stage_tags_json=["S1", "seo", "competitor"],
        )

        from ..engine.quality_gates import get_quality_gates
        report = get_quality_gates().validate(insight)
        if not report.passed:
            return None
        saved = await store.create_insight(insight)
        self.bus.emit("insight.created", {
            "insight_id": saved.id, "type": saved.type, "title": saved.title,
        })
        return saved

    # ========== CI-7 竞品动向周报 ==========

    async def build_weekly_report(self, monitor_summaries: list[dict] | None = None) -> str:
        """聚合本周竞品信号（变更监控 + SEO + 洞察）→ 《竞品动向》周报"""
        store = await get_store()
        now = datetime.now(timezone.utc)
        week_ago = now - timedelta(days=7)

        insights = await store.list_insights(self.workspace_id, limit=200)
        competitor_insights = [
            i for i in insights
            if i.type in ("competitor_pricing", "site_change", "keyword_gap")
            and i.created_at >= week_ago
        ]

        # 本周最值得注意的 3 件事（按 severity + 时间加权）
        ranked = sorted(
            competitor_insights,
            key=lambda i: (
                {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}[i.severity.value],
                i.created_at,
            ),
            reverse=True,
        )
        top3 = ranked[:3]

        lines = [
            "# 竞品动向周报",
            "",
            "| 项 | 内容 |",
            "|---|---|",
            f"| 工作区 | {self.workspace_id} |",
            f"| 周期 | {week_ago.strftime('%Y-%m-%d')} ~ {now.strftime('%Y-%m-%d')} |",
            f"| 竞品信号 | {len(competitor_insights)} 条 |",
            "",
            "## 本周最值得注意的 3 件事",
            "",
        ]
        if top3:
            for i, ins in enumerate(top3, 1):
                lines.append(f"{i}. 【{ins.severity.value.upper()}】{ins.title} [ins:{ins.id}]")
        else:
            lines.append("本周竞品面无显著异动。")

        if monitor_summaries:
            lines += ["", "## 监控项摘要", ""]
            for ms in monitor_summaries:
                lines.append(f"- {ms}")

        # 竞品档案清单
        profiles = await store.list_competitors(self.workspace_id)
        if profiles:
            lines += ["", "## 竞品档案", ""]
            for p in profiles:
                lines.append(f"- **{p['name']}**（{p['domain']}）：{p['positioning'] or '未填写定位'}")

        return "\n".join(lines)

    async def publish_weekly_report(self, monitor_summaries: list[dict] | None = None) -> str:
        """生成并落盘周报"""
        report_md = await self.build_weekly_report(monitor_summaries)
        path = ReportStore(self.workspace_id).save_report("competitors", report_md)
        self.bus.emit("report.ready", {"kind": "competitors_weekly", "path": str(path)})
        return str(path)

    # ========== 网站变更监控（CI-2 联动）==========

    def change_monitor(self) -> ChangeMonitor:
        return ChangeMonitor(self.workspace_id)


def theirs_ranks(theirs: list[dict]) -> list[dict]:
    """辅助：过滤有效排名"""
    return [r for r in theirs if r.get("position") and r.get("keyword")]
