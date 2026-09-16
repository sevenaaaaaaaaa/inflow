"""Insight Flow 网站变更监控 MVP

定价页 + changelog 监控：Firecrawl 抓取 → 快照落盘 → 语义 diff → 洞察生成
快照存 data/snapshots/{workspace}/{monitor_id}/YYYY-MM-DD/content.html
"""

import re
import difflib
from datetime import datetime, timezone

from ..core.files import EventBus, SnapshotStore
from ..core.store import get_store
from ..core.entities import Insight, InsightAction

# 价格数字模式（$49 / ¥499 / 99 USD / $1,299.00 等）
PRICE_PATTERN = re.compile(
    r"(?:[$¥€£]|USD|CNY|RMB)\s?([\d,]+(?:\.\d+)?)", re.IGNORECASE
)


def extract_prices(markdown: str) -> list[float]:
    """从页面 markdown 提取价格数字"""
    prices = []
    for m in PRICE_PATTERN.finditer(markdown):
        try:
            prices.append(float(m.group(1).replace(",", "")))
        except ValueError:
            continue
    return prices


class ChangeMonitor:
    """网站变更监控

    流程：抓取当前页 → 与最近快照 diff → 分类变更（价格/内容/结构）
    → 变更显著则生成 Insight（带证据：diff 摘要）
    """

    # 内容变化比例阈值：超过则判定为显著变更
    SIGNIFICANT_RATIO = 0.05

    def __init__(self, workspace_id: str):
        self.workspace_id = workspace_id
        self.bus = EventBus(workspace_id)

    def semantic_diff(self, old_md: str, new_md: str) -> dict:
        """语义 diff：价格变化 + 内容变化比例 + 变更行摘要"""
        old_prices = extract_prices(old_md)
        new_prices = extract_prices(new_md)

        price_changes = []
        if old_prices and new_prices:
            # 对齐相同位置的价格（简单策略：数量相同时逐位比较）
            if len(old_prices) == len(new_prices):
                for old_p, new_p in zip(old_prices, new_prices):
                    if old_p != new_p and old_p > 0:
                        price_changes.append({
                            "old": old_p,
                            "new": new_p,
                            "pct": round((new_p - old_p) / old_p, 4),
                        })

        # 内容相似度
        ratio = difflib.SequenceMatcher(None, old_md, new_md).ratio()
        changed_ratio = 1.0 - ratio

        # 变更行摘要（unified diff 前 N 行）
        diff_lines = list(difflib.unified_diff(
            old_md.splitlines(), new_md.splitlines(), lineterm="", n=1
        ))
        changed_lines = [l for l in diff_lines if l.startswith(("+ ", "- "))][:20]

        return {
            "price_changes": price_changes,
            "changed_ratio": round(changed_ratio, 4),
            "significant": changed_ratio >= self.SIGNIFICANT_RATIO or bool(price_changes),
            "diff_excerpt": "\n".join(changed_lines),
        }

    async def check(
        self,
        monitor_id: str,
        url: str,
        current_markdown: str,
        cost: dict | None = None,
    ) -> dict:
        """执行一次变更检查

        current_markdown: 刚抓取的页面内容（由 Firecrawl 插件采集）
        """
        snapshots = SnapshotStore(self.workspace_id)
        latest = snapshots.get_latest_snapshot(monitor_id)

        result = {
            "monitor_id": monitor_id,
            "url": url,
            "first_run": latest is None,
            "changed": False,
            "significant": False,
        }

        if latest:
            old_path, old_content = latest
            diff = self.semantic_diff(old_content, current_markdown)
            result.update({
                "changed": diff["changed_ratio"] > 0,
                "significant": diff["significant"],
                "diff": diff,
            })

            if diff["significant"]:
                insight = await self._insight_from_diff(monitor_id, url, diff)
                result["insight_id"] = insight.id if insight else None

        # 无论是否变更都保存今日快照（同日覆盖）
        snapshots.save_snapshot(monitor_id, current_markdown)
        self.bus.emit("monitor.run_finished", {
            "monitor_id": monitor_id,
            "kind": "site_change",
            "url": url,
            "changed": result["changed"],
            "significant": result["significant"],
            "cost": cost or {},
        })
        return result

    async def _insight_from_diff(self, monitor_id: str, url: str, diff: dict) -> Insight | None:
        """变更 → 洞察（价格异动/内容大改）"""
        store = await get_store()
        price_changes = diff.get("price_changes", [])

        if price_changes:
            biggest = max(price_changes, key=lambda c: abs(c["pct"]))
            direction = "涨价" if biggest["pct"] > 0 else "降价"
            title = f"竞品定价页变动：{direction} {abs(biggest['pct']):.0%}"
            summary = (
                f"监控到 {url} 定价页发生变动："
                f"价格从 {biggest['old']} 变为 {biggest['new']}（{biggest['pct']:+.0%}）。"
                f"共 {len(price_changes)} 处价格变动。"
            )
            severity = "high" if abs(biggest["pct"]) >= 0.15 else "medium"
            confidence = 0.85
            ev = [{
                "type": "price_diff",
                "monitor_id": monitor_id,
                "url": url,
                "changes": price_changes,
            }]
        else:
            title = f"竞品页面大幅变更：{url}"
            summary = (
                f"监控到 {url} 内容变化 {diff['changed_ratio']:.0%}，"
                "可能涉及产品/信息结构调整，建议人工复核。"
            )
            severity = "medium"
            confidence = 0.7
            ev = [{
                "type": "content_diff",
                "monitor_id": monitor_id,
                "url": url,
                "changed_ratio": diff["changed_ratio"],
                "diff_excerpt": diff["diff_excerpt"][:2000],
            }]

        insight = Insight(
            workspace_id=self.workspace_id,
            type="competitor_pricing" if price_changes else "site_change",
            title=title,
            summary=summary,
            severity=severity,
            confidence=confidence,
            evidence_json=ev,
            models_json=["site_change_monitor"],
            actions_json=[
                {
                    "action_type": "investigate",
                    "description": "复核竞品变更详情并评估影响",
                },
                {
                    "action_type": "mflow.create_content",
                    "description": "针对竞品变动产出应对内容（对比/评测角度）",
                },
                {
                    "action_type": "openflow.webhook_insight",
                    "description": "推送洞察到 OpenFlow CDP",
                },
            ],
            stage_tags_json=["competitor", "monitoring"],
        )

        # 质量门
        from ..engine.quality_gates import get_quality_gates
        report = get_quality_gates().validate(insight)
        if not report.passed:
            self.bus.emit("insight.quality_gate_blocked", {
                "title": title, "errors": report.errors,
            })
            return None

        saved = await store.create_insight(insight)
        self.bus.emit("insight.created", {
            "insight_id": saved.id,
            "type": saved.type,
            "title": saved.title,
        })
        return saved
