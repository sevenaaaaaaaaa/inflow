"""Insight Flow 运营邮件日报（清晨 / 傍晚，发管理员）

每天两封（本地时区 08:00 / 18:00，可用 INSFLOW_DIGEST_HOURS 覆盖）：
- 过去 12 小时：任务执行情况（成功率/失败/跳过）、流量情况（采集量 + 第一方指标）、
  转化情况（洞察→动作→验证的漏斗进展）
- 当日：任务执行汇总、token/成本消耗（每小时分布）、每日预算状态

收件人：工作区设置 alert_targets.email.email_to，兜底 INSFLOW_ADMIN_EMAIL。
SMTP 未配置时发送会明确失败并落事件流（email.digest_failed），不静默丢。
"""

import logging
from datetime import UTC, datetime, timedelta

from ..core.files import EventBus
from ..core.work_calendar import local_date, local_tzname
from .usage_ledger import DailyBudget, UsageLedger

logger = logging.getLogger("insflow.email_digest")

# 第一方流量/转化指标（metric_daily 里聚合；缺数据时日报该栏留空不硬造）
TRAFFIC_METRICS = ("ga4_sessions", "gsc_clicks")
CONVERSION_METRICS = ("ga4_conversions",)


class OpsDigestBuilder:
    """运营日报（邮件）：过去12h 执行/流量/转化 + 当日任务与 token 消耗"""

    def __init__(self, workspace_id: str):
        self.workspace_id = workspace_id or "default"
        self.bus = EventBus(self.workspace_id)

    # ========== 数据聚合 ==========

    async def build(self, window_hours: int = 12) -> dict:
        events = self._events(window_hours)

        execution = self._execution(events, window_hours)
        traffic = await self._traffic(events, window_hours)
        conversion = await self._conversion(events, window_hours)
        today = await self._today()

        md = self._render(execution, traffic, conversion, today)
        self.bus.emit("report.ready", {"kind": "ops_digest", "window_hours": window_hours})
        return {"markdown": md,
                "summary": {"execution": execution, "traffic": traffic,
                            "conversion": conversion, "today": today}}

    def _events(self, window_hours: int) -> list[dict]:
        since = datetime.now(UTC) - timedelta(hours=window_hours)
        out = []
        for e in self.bus.read(limit=2000):
            try:
                if datetime.fromisoformat(e["ts"]) >= since:
                    out.append(e)
            except (KeyError, ValueError, TypeError):
                continue
        return out

    def _execution(self, events: list[dict], window_hours: int) -> dict:
        """过去 N 小时任务执行情况（监控 + Agent 任务 + 日历跳过）"""
        ok = sum(1 for e in events if e["type"] == "monitor.run_finished")
        fail = sum(1 for e in events if e["type"] == "monitor.alert")
        skipped = sum(1 for e in events if e["type"] == "monitor.skipped")
        agent_ok = sum(1 for e in events if e["type"] == "agent.task.run.finished")
        agent_fail = sum(1 for e in events if e["type"] == "agent.task.run.failed")
        total = ok + fail
        errors = [f"{(e.get('payload') or {}).get('monitor_id') or '?'}: "
                  f"{str((e.get('payload') or {}).get('error', ''))[:80]}"
                  for e in events if e["type"] == "monitor.alert"][-5:]
        return {
            "window_hours": window_hours,
            "monitor_ok": ok, "monitor_fail": fail, "skipped_non_workday": skipped,
            "success_rate": (ok / total) if total else None,
            "agent_ok": agent_ok, "agent_fail": agent_fail,
            "errors": errors,
        }

    async def _traffic(self, events: list[dict], window_hours: int) -> dict:
        """流量情况：采集动作次数 + 第一方指标（GSC/GA4，metric_daily 口径）"""
        collected = sum(1 for e in events if e["type"] == "source.collected")
        first_party: dict[str, dict] = {}
        try:
            from ..core.store import get_store
            store = await get_store()
            today = local_date().isoformat()
            yesterday = (local_date() - timedelta(days=1)).isoformat()
            placeholders = ",".join("?" for _ in TRAFFIC_METRICS)
            rows = await store._fetchall(
                f"""SELECT metric, day, SUM(agg_sum) AS v FROM metric_daily
                    WHERE workspace_id = ? AND metric IN ({placeholders})
                      AND day IN (?, ?)
                    GROUP BY metric, day""",
                (self.workspace_id, *TRAFFIC_METRICS, yesterday, today))
            for r in rows:
                first_party.setdefault(r["metric"], {})[r["day"]] = float(r["v"] or 0)
        except Exception:
            logger.exception("流量指标聚合失败（日报该栏留空）")
        return {"collected_events": collected, "first_party": first_party}

    async def _conversion(self, events: list[dict], window_hours: int) -> dict:
        """转化情况：洞察 → 动作 → 验证的漏斗进展（12h 内）+ GA4 转化数"""
        insights = sum(1 for e in events if e["type"] == "insight.created")
        dispatched = sum(1 for e in events if e["type"] == "action.dispatched")
        executed = sum(1 for e in events if e["type"] == "action.executed")
        verified = sum(1 for e in events if e["type"] == "feedback.received")
        ga4: dict[str, dict] = {}
        try:
            from ..core.store import get_store
            store = await get_store()
            today = local_date().isoformat()
            yesterday = (local_date() - timedelta(days=1)).isoformat()
            placeholders = ",".join("?" for _ in CONVERSION_METRICS)
            rows = await store._fetchall(
                f"""SELECT metric, day, SUM(agg_sum) AS v FROM metric_daily
                    WHERE workspace_id = ? AND metric IN ({placeholders})
                      AND day IN (?, ?)
                    GROUP BY metric, day""",
                (self.workspace_id, *CONVERSION_METRICS, yesterday, today))
            for r in rows:
                ga4.setdefault(r["metric"], {})[r["day"]] = float(r["v"] or 0)
        except Exception:
            logger.exception("转化指标聚合失败（日报该栏留空）")
        return {"insights": insights, "actions_dispatched": dispatched,
                "actions_executed": executed, "verifications": verified, "ga4": ga4}

    async def _today(self) -> dict:
        """当日：任务执行汇总 + token 消耗（含每小时分布）+ 预算状态 + 消耗大户"""
        ledger = UsageLedger(self.workspace_id)
        budget = DailyBudget(self.workspace_id)
        today_events = self._events(24)
        return {
            "date": local_date().isoformat(),
            "monitors": sum(1 for e in today_events
                            if e["type"] == "monitor.run_finished"),
            "monitor_fails": sum(1 for e in today_events
                                 if e["type"] == "monitor.alert"),
            "usage": ledger.today(),
            "hourly": [h for h in ledger.hourly() if h["calls"]],
            "top_consumers": ledger.top_consumers(days=1, limit=3),
            "budget": await budget.status(refresh=True),
        }

    # ========== 渲染与发送 ==========

    def _render(self, execution: dict, traffic: dict, conversion: dict, today: dict) -> str:
        rate = execution["success_rate"]
        rate_str = f"{rate:.0%}" if rate is not None else "窗口内无执行"
        u = today["usage"]
        b = today["budget"]
        spent = b["spent"]

        lines = [
            "# Insight Flow 运营日报",
            "",
            f"窗口：过去 {execution['window_hours']} 小时 · "
            f"本地时区 {local_tzname()} · 生成于 {datetime.now(UTC).strftime('%m-%d %H:%M UTC')}",
            "",
            "## 过去 12 小时",
            "",
            "### 执行情况",
            f"- 监控任务：成功 {execution['monitor_ok']} / 失败 {execution['monitor_fail']}"
            f"（成功率 {rate_str}）",
            f"- 日历跳过（非工作日/节假日）：{execution['skipped_non_workday']} 次",
            f"- Agent 定时任务：成功 {execution['agent_ok']} / 失败 {execution['agent_fail']}",
        ]
        if execution["errors"]:
            lines.append("- 最近失败：")
            lines += [f"  - {err}" for err in execution["errors"]]

        lines += ["", "### 流量情况",
                  f"- 数据源采集：{traffic['collected_events']} 次"]
        fp = traffic["first_party"]
        if fp:
            for metric, days in fp.items():
                today_v = days.get(today["date"])
                y_v = days.get((local_date() - timedelta(days=1)).isoformat())
                today_s = f"{today_v:.0f}" if today_v is not None else "—"
                y_s = f"{y_v:.0f}" if y_v is not None else "—"
                lines.append(f"- {metric}：今日 {today_s}（昨日 {y_s}）")
        else:
            lines.append("- 第一方指标（GSC/GA4）暂无数据")

        lines += ["", "### 转化情况",
                  f"- 新增洞察：{conversion['insights']} 条",
                  f"- 动作派发/执行：{conversion['actions_dispatched']} / {conversion['actions_executed']}",
                  f"- 验证结论回流：{conversion['verifications']} 条"]
        for metric, days in (conversion.get("ga4") or {}).items():
            today_v = days.get(today["date"])
            today_s = f"{today_v:.0f}" if today_v is not None else "—"
            lines.append(f"- {metric}（GA4）：今日 {today_s}")

        lines += [
            "",
            "## 当日汇总",
            "",
            "### 任务执行",
            f"- 监控任务完成 {today['monitors']} 次（失败 {today['monitor_fails']} 次）",
            "",
            "### Token 消耗",
            f"- LLM 调用：{u['calls']} 次，输入 {u['prompt_tokens']} tokens / "
            f"输出 {u['completion_tokens']} tokens，成本 ${u['cost_usd']}"
            f"（约 ¥{u['cost_cny']}）",
        ]
        hourly = today.get("hourly") or []
        if hourly:
            lines.append("- 每小时分布：" + "；".join(
                f"{h['hour']:02d}时 {h['calls']}次/${h['cost_usd']}" for h in hourly))
        top = (today.get("top_consumers") or {}).get("by_model") or []
        if top:
            lines.append("- 消耗大户（今日·模型）：" + "、".join(
                f"{t['name']} ${t['cost_usd']}" for t in top[:3]))

        lines += [
            "",
            "### 预算状态",
            f"- 今日已花 ¥{spent['total_cost_cny']} / 预算 ¥{b['budget_cny']}"
            f"（{b['used_ratio']:.0%}）",
            f"- LLM ${spent['llm_cost_usd']} + 数据源 ${spent['source_cost_usd']}",
        ]
        if b["exceeded"]:
            lines.append("- ⚠️ 已超支：非重要任务的 LLM 调用已熔断，"
                         "可在控制台「自动化运营」单日加预算（次日自动回调）")
        elif b["warning"]:
            lines.append("- 注意：已用到预算的 80%+")
        return "\n".join(lines)

    async def send(self, window_hours: int = 12, period: str = "morning") -> dict:
        """生成日报 → 邮件发管理员（收件人未配置时落事件流，不静默）"""
        built = await self.build(window_hours)
        label = "清晨" if period == "morning" else "傍晚"
        from .usage_ledger import send_admin_email
        result = await send_admin_email(
            self.workspace_id,
            title=f"运营日报（{label}）：过去 {window_hours}h 执行 / 流量 / 转化 + 当日消耗",
            body=built["markdown"], severity="info")
        if not result.get("ok"):
            self.bus.emit("email.digest_failed", {
                "period": period, "error": str(result.get("error", ""))[:200]})
        return {"period": period, **result, "summary": built["summary"]}


# ========== 定时入口（bootstrap 注册）==========

def digest_crons() -> list[tuple[str, str]]:
    """日报触发时刻：本地 08:00（清晨）与 18:00（傍晚），UTC cron

    INSFLOW_DIGEST_HOURS="8,18" 可覆盖（本地小时，逗号分隔，最多 2 个）。
    """
    import os

    from ..core.work_calendar import local_hour_to_utc_cron
    raw = os.environ.get("INSFLOW_DIGEST_HOURS", "8,18").strip()
    hours: list[int] = []
    for piece in raw.replace("；", ",").replace("，", ",").split(","):
        piece = piece.strip()
        if piece.isdigit() and 0 <= int(piece) <= 23:
            hours.append(int(piece))
    if not hours:
        hours = [8, 18]
    hours = sorted(set(hours))[:2]
    labels = ["morning", "evening"]
    return [(labels[i], local_hour_to_utc_cron(h, 0)) for i, h in enumerate(hours)]
