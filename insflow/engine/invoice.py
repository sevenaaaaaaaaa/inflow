"""Insight Flow 计费对账单（R3-3）

月度用量 → 客户可读对账单（Markdown，人工确认后归档）。
数据源：BillingManager.usage_summary（用量 API 同源，口径一致）。

与真实支付系统对接时：本模块输出对账单，支付回调由云托管计费系统负责（V2）。
"""

from datetime import datetime, timezone

from ..core.files import EventBus, ReportStore
from .billing import BillingManager, PLANS


class InvoiceBuilder:
    """对账单生成器"""

    def __init__(self, workspace_id: str):
        self.workspace_id = workspace_id

    async def build(self, month: str | None = None,
                    usage_records: dict | None = None) -> dict:
        """生成月度对账单

        usage_records: 当月用量记录（BillingManager.record_usage 聚合），
                       留空则用套餐默认限额作为展示基线。
        """
        mgr = BillingManager(self.workspace_id)
        summary = await mgr.usage_summary()
        usage = usage_records or {}

        plan_id = summary["plan_id"]
        plan = PLANS[plan_id]
        price = plan["price_usd_month"]

        md = self._render(month=month or datetime.now(timezone.utc).strftime("%Y-%m"),
                          plan_name=summary["plan_name"], price=price,
                          usage=usage, features=summary["features"])

        path = ReportStore(self.workspace_id).save_report("invoices", md)
        EventBus(self.workspace_id).emit("invoice.ready", {
            "month": month, "plan": plan_id,
        })
        return {"invoice_path": str(path), "markdown": md, "summary": summary}

    def _render(self, month: str, plan_name: str, price, usage: dict,
                features: list[str]) -> str:
        lines = [
            f"# Insight Flow 对账单 · {month}",
            "",
            "| 项 | 内容 |",
            "|---|---|",
            f"| 账期 | {month} |",
            f"| 工作区 | {self.workspace_id} |",
            f"| 套餐 | {plan_name} |",
            f"| 月费 | {'报价制（按合同）' if price is None else f'${price}'} |",
            "",
            "## 用量明细",
            "",
            "| 项 | 本期用量 |",
            "|---|---|",
        ]
        metric_names = {
            "api_calls": "API 调用次数",
            "agent_asks": "Agent 问答次数",
            "deep_reports": "深度报告份数",
            "cost_usd": "数据源费用（USD）",
        }
        for kind, label in metric_names.items():
            val = usage.get(kind, "—")
            if kind == "cost_usd" and isinstance(usage.get(kind), (int, float)):
                usage_val = f"${usage[kind]:.2f}"
            else:
                usage_val = usage.get(kind, "未记录（见用量 API）")
            lines.append(f"| {label} | {usage_val} |")

        lines += ["", "## 套餐包含", ""]
        for f in features:
            lines.append(f"- {f}")

        lines += [
            "",
            "> 本对账单由用量 API 自动生成；如与实际不符请联系客服复核。",
            "",
        ]
        return "\n".join(lines)
