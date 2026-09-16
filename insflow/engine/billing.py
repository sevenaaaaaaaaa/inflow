"""Insight Flow 计费与配额产品化（M5）

套餐档位（对齐 PRD 定价表 + 数据源矩阵 L1/L2/L3）：
- free：单 Workspace、L1 免费底座（GSC/GA4/CrUX/GDELT/Reddit/知乎）、OpenFlow/MFlow 集成
- growth：+ 竞品监控 3 个、DataForSEO 额度、Agent 问答、周报
- scale：+ 竞品 20 个、广告情报、多渠道告警、API/MCP、白标报告
- enterprise：私有化部署、L3 企业源（客户自有凭据）、SSO、审计

配额产品化：用量可见（API）+ 超量告警（events.jsonl → webhook/飞书出站链路复用）
"""

from datetime import datetime, timezone
from typing import Any

from ..core.files import EventBus
from ..core.store import get_store

# ========== 套餐定义（单一事实源）==========

PLANS: dict[str, dict[str, Any]] = {
    "free": {
        "name": "Free / 自托管开源",
        "price_usd_month": 0,
        "sources": ["gsc", "ga4", "crux", "gdelt", "reddit", "zhihu", "serper", "firecrawl"],
        "features": ["openflow_integration", "mflow_integration", "diagnosis", "maturity"],
        "limits": {
            "workspaces": 1,
            "competitors": 0,          # 免费档无竞品监控
            "monthly_api_calls": 2000,
            "monthly_cost_usd": 5.0,
            "agent_asks": 0,           # 无 Agent 问答
            "deep_reports": 0,
            "white_label": False,
        },
    },
    "growth": {
        "name": "Growth",
        "price_usd_month": 149,
        "sources": ["gsc", "ga4", "crux", "gdelt", "reddit", "zhihu",
                    "serper", "brave", "dataforseo-serp", "dataforseo-labs", "firecrawl"],
        "features": ["agent_ask", "weekly_report", "site_change_monitor", "dsl_models"],
        "limits": {
            "workspaces": 3,
            "competitors": 3,
            "monthly_api_calls": 20000,
            "monthly_cost_usd": 50,
            "agent_asks": 200,
            "deep_reports": 2,
            "white_label": False,
        },
    },
    "scale": {
        "name": "Scale",
        "price_usd_month": 699,
        "sources": ["gsc", "ga4", "crux", "gdelt", "reddit", "zhihu",
                    "serper", "brave", "tavily",
                    "dataforseo-serp", "dataforseo-labs", "dataforseo-ads", "firecrawl"],
        "features": ["agent_ask", "weekly_report", "ad_intelligence", "multi_channel_alerts",
                     "api_mcp", "white_label", "dsl_models", "deep_reports"],
        "limits": {
            "workspaces": 10,
            "competitors": 20,
            "monthly_api_calls": 100000,
            "monthly_cost_usd": 250,
            "agent_asks": 1000,
            "deep_reports": 10,
            "white_label": True,
        },
    },
    "enterprise": {
        "name": "Enterprise / 私有化",
        "price_usd_month": None,  # 年费报价制
        "sources": ["*"],  # L2 全量 + 客户自有凭据的企业源（semrush/similarweb/brandwatch/新榜/清博）
        "features": ["*"],
        "limits": {
            "workspaces": None,        # 不限
            "competitors": 100,
            "monthly_api_calls": None,
            "monthly_cost_usd": None,
            "agent_asks": None,
            "deep_reports": None,
            "white_label": True,
        },
    },
}

DEFAULT_PLAN = "free"

# 告警阈值（80% 预警，100% 熔断提示）
WARNING_RATIO = 0.8


class QuotaExceededForPlan(Exception):
    """套餐配额不足"""
    pass


class BillingManager:
    """套餐与配额管理（租户级）"""

    def __init__(self, workspace_id: str):
        self.workspace_id = workspace_id
        self._usage: dict[str, int | float] = {}

    # ========== 套餐绑定 ==========

    async def get_plan_id(self) -> str:
        store = await get_store()
        ws = await store.get_workspace(self.workspace_id)
        if not ws:
            return DEFAULT_PLAN
        return (ws.settings_json or {}).get("plan", DEFAULT_PLAN)

    async def get_plan(self) -> dict:
        plan_id = await self.get_plan_id()
        plan = dict(PLANS.get(plan_id, PLANS[DEFAULT_PLAN]))
        plan["plan_id"] = plan_id
        return plan

    async def set_plan(self, plan_id: str) -> dict:
        """切换套餐（云托管计费系统调用）"""
        if plan_id not in PLANS:
            raise QuotaExceededForPlan(f"未知套餐: {plan_id}")
        store = await get_store()
        ws = await store.get_workspace(self.workspace_id)
        if not ws:
            raise QuotaExceededForPlan(f"Workspace 不存在: {self.workspace_id}")
        settings = dict(ws.settings_json or {})
        settings["plan"] = plan_id
        ws.settings_json = settings
        await store.update_workspace(ws)
        return {"workspace_id": self.workspace_id, "plan_id": plan_id}

    # ========== 用量记录（运营动作统一走这里）==========

    def record_usage(self, kind: str, amount: int | float = 1) -> None:
        """记录用量：api_calls / agent_asks / deep_reports / api_cost_usd ..."""
        self._usage[kind] = self._usage.get(kind, 0) + amount

    def current_usage(self) -> dict:
        return dict(self._usage)

    # ========== 配额检查（配额产品化核心）==========

    async def check_quota(self, kind: str, requested: int | float = 1) -> dict:
        """检查配额：返回 {allowed, ratio, warnings}

        超量语义：80% 预警（quota.warning 事件），100% 拒绝。
        limits 中该 kind 为 None 表示不限量（Enterprise）。
        """
        plan = await self.get_plan()
        limits = plan["limits"]

        limit_map = {
            "api_calls": "monthly_api_calls",
            "cost_usd": "monthly_cost_usd",
            "agent_asks": "agent_asks",
            "deep_reports": "deep_reports",
            "competitors": "competitors",
            "workspaces": "workspaces",
        }
        limit_key = limit_map.get(kind)
        if not limit_key:
            return {"allowed": True, "ratio": 0.0}
        limit = limits.get(limit_key)
        if limit is None:
            return {"allowed": True, "ratio": 0.0, "unlimited": True}

        used = self._usage.get(kind, 0)
        ratio = (used + requested) / limit if limit else 0
        allowed = used + requested <= limit

        if allowed and ratio >= WARNING_RATIO:
            self.bus_emit("quota.warning", {
                "kind": kind, "used": used + requested, "limit": limit,
                "ratio": round(ratio, 2),
            })
        return {"allowed": allowed, "ratio": round(ratio, 3), "used": used, "limit": limit}

    # ========== 用量可见（PL 计费产品化）==========

    async def usage_summary(self, source_usage: dict | None = None) -> dict:
        """套餐 + 用量 + 限额百分比（前端用量页直接渲染）"""
        plan = await self.get_plan()
        limits = plan["limits"]

        api_used = self._usage.get("api_calls", 0)
        agent_used = self._usage.get("agent_asks", 0)
        deep_used = self._usage.get("deep_reports", 0)
        cost_used = self._usage.get("cost_usd", 0.0)

        def _pct(used, limit):
            return round(used / limit, 3) if limit else 0.0

        return {
            "workspace_id": self.workspace_id,
            "plan_id": plan["plan_id"],
            "plan_name": plan["name"],
            "price_usd_month": plan["price_usd_month"],
            "features": plan["features"],
            "sources_allowed": plan["sources"],
            "usage": {
                "api_calls": {"used": api_used, "limit": limits["monthly_api_calls"],
                              "pct": _pct(api_used, limits["monthly_api_calls"])},
                "agent_asks": {"used": agent_used, "limit": limits["agent_asks"],
                               "pct": _pct(agent_used, limits["agent_asks"])},
                "deep_reports": {"used": deep_used, "limit": limits["deep_reports"],
                                 "pct": _pct(deep_used, limits["deep_reports"])},
                "cost_usd": {"used": round(cost_used, 2), "limit": limits["monthly_cost_usd"],
                             "pct": _pct(cost_used, limits["monthly_cost_usd"])},
                "competitors": {"limit": limits["competitors"]},
                "white_label": limits["white_label"],
            },
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    def bus_emit(self, event_type: str, payload: dict) -> None:
        from ..core.files import EventBus
        EventBus(self.workspace_id).emit(event_type, payload)

    # ========== 超量告警出站（复用告警链路）==========

    async def alert_on_overage(self, kind: str, used: float, limit: float) -> None:
        """配额超限 → quota.exceeded 事件（webhook/飞书订阅即收）"""
        self.bus_emit("quota.exceeded", {
            "kind": kind, "used": used, "limit": limit,
            "plan": await self.get_plan_id(),
        })
