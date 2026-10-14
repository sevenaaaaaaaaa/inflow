"""Insight Flow Token 用量账本（每日 / 每小时 / 按模型 / 按任务）

背景：此前 LLM 记账只有「会话账本 + 月度累计」（llm.py BudgetLedger +
settings_json.usage.<月>.llm_cost_usd），回答不了两个问题：
- 今天花了多少？哪个小时花的？（日报与预算熔断需要日/小时粒度）
- 钱花在哪个模型/哪个任务上？（找消耗大户需要维度聚合）

设计（文件优先、轻依赖）：每工作区一个 JSONL 追加文件
    data/usage/<workspace_id>/llm_usage.jsonl
每行一条调用记录（UTC 时间戳 + 模型/任务/tokens/cost），读侧聚合出：
- daily(days)：按本地日期聚合（近 N 天趋势）
- hourly(date)：某日 24 小时分布
- by_model / by_task：消耗大户
- today(): 今日累计（本地日期口径，预算熔断与日报共用）

附：每日预算（默认 ¥5）+ 超支邮件告警 + 管理员单日加预算（次日自动回调）
见 DailyBudget 类——预算口径 = 本账本 LLM 成本 + source.collected 事件里的
数据源成本（有 cost 字段就记，没有不硬算）。
"""

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..core.files import DATA_DIR
from ..core.work_calendar import local_date

logger = logging.getLogger("insflow.usage_ledger")

KEEP_DAYS = 90  # 账本保留天数（与事件流轮转同思路，防无限增长）


class UsageLedger:
    """LLM 用量账本（JSONL 追加 + 读侧聚合）"""

    def __init__(self, workspace_id: str, base_dir: Path | None = None):
        self.workspace_id = workspace_id or "default"
        base = base_dir or DATA_DIR
        self.usage_dir = base / "usage" / self.workspace_id
        self.usage_dir.mkdir(parents=True, exist_ok=True)
        self.file = self.usage_dir / "llm_usage.jsonl"

    # ========== 写入 ==========

    def record(self, *, model: str = "", task: str = "", tier: str = "",
               prompt_tokens: int = 0, completion_tokens: int = 0,
               cost_usd: float = 0.0, latency_ms: int = 0,
               degraded: bool = False, important: bool = False) -> dict:
        """追加一条调用记录（追加失败不影响主流程——记账是旁路）"""
        row = {
            "ts": datetime.now(UTC).isoformat(),
            "model": model, "task": task, "tier": tier,
            "prompt_tokens": int(prompt_tokens),
            "completion_tokens": int(completion_tokens),
            "cost_usd": round(float(cost_usd), 6),
            "latency_ms": int(latency_ms),
            "degraded": bool(degraded),
            "important": bool(important),
        }
        try:
            with open(self.file, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            logger.exception("用量账本写入失败")
        return row

    def _rows(self, since: datetime | None = None) -> list[dict]:
        if not self.file.exists():
            return []
        out: list[dict] = []
        try:
            with open(self.file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                        if since and row.get("ts", "") < since.isoformat():
                            continue
                        out.append(row)
                    except (json.JSONDecodeError, TypeError, ValueError):
                        continue
        except OSError:
            return []
        return out

    # ========== 聚合（本地日期口径）==========

    def today(self) -> dict:
        """今日累计（本地日期；tokens/calls/cost）"""
        return self.daily(days=1)[0] if self.daily(days=1) else {
            "date": local_date().isoformat(), "calls": 0, "prompt_tokens": 0,
            "completion_tokens": 0, "cost_usd": 0.0, "cost_cny": 0.0,
        }

    def daily(self, days: int = 14) -> list[dict]:
        """近 N 天每日聚合（含今天；无数据的天也返回 0 行，前端画趋势不断轴）"""
        rate = usd_cny_rate()
        rows = self._rows()
        by_day: dict[str, dict] = {}
        today = local_date()
        for i in range(days - 1, -1, -1):
            d = (today - timedelta(days=i)).isoformat()
            by_day[d] = {"date": d, "calls": 0, "prompt_tokens": 0,
                         "completion_tokens": 0, "cost_usd": 0.0}
        for r in rows:
            d = _local_date_of(r.get("ts", ""))
            if d and d in by_day:
                agg = by_day[d]
                agg["calls"] += 1
                agg["prompt_tokens"] += r.get("prompt_tokens", 0)
                agg["completion_tokens"] += r.get("completion_tokens", 0)
                agg["cost_usd"] += r.get("cost_usd", 0.0)
        out = []
        for agg in by_day.values():
            agg["cost_usd"] = round(agg["cost_usd"], 4)
            agg["cost_cny"] = round(agg["cost_usd"] * rate, 2)
            out.append(agg)
        return out

    def hourly(self, d: str | None = None) -> list[dict]:
        """某日 24 小时分布（本地日期；缺省今天）"""
        rows = self._rows()
        target = d or local_date().isoformat()
        buckets = [{"hour": h, "calls": 0, "prompt_tokens": 0,
                    "completion_tokens": 0, "cost_usd": 0.0} for h in range(24)]
        for r in rows:
            if _local_date_of(r.get("ts", "")) != target:
                continue
            h = _local_hour_of(r.get("ts", ""))
            if h is None:
                continue
            buckets[h]["calls"] += 1
            buckets[h]["prompt_tokens"] += r.get("prompt_tokens", 0)
            buckets[h]["completion_tokens"] += r.get("completion_tokens", 0)
            buckets[h]["cost_usd"] += r.get("cost_usd", 0.0)
        for b in buckets:
            b["cost_usd"] = round(b["cost_usd"], 4)
        return buckets

    def by_model(self, days: int = 7) -> list[dict]:
        """按模型聚合（消耗大户：模型维度）"""
        return self._top(self._since(days), "model", days)

    def by_task(self, days: int = 7) -> list[dict]:
        """按任务类型聚合（消耗大户：任务维度）"""
        return self._top(self._since(days), "task", days)

    def top_consumers(self, days: int = 7, limit: int = 5) -> dict:
        """消耗大户总览（模型 + 任务两个维度，进日报与运营页）"""
        return {
            "days": days,
            "by_model": self.by_model(days)[:limit],
            "by_task": self.by_task(days)[:limit],
        }

    # ---------- 内部 ----------

    def _since(self, days: int) -> list[dict]:
        since = datetime.now(UTC) - timedelta(days=days)
        return self._rows(since)

    def _top(self, rows: list[dict], key: str, days: int) -> list[dict]:
        agg: dict[str, dict] = {}
        for r in rows:
            name = r.get(key) or "(unknown)"
            row = agg.setdefault(name, {"calls": 0, "prompt_tokens": 0,
                                        "completion_tokens": 0, "cost_usd": 0.0})
            row["calls"] += 1
            row["prompt_tokens"] += r.get("prompt_tokens", 0)
            row["completion_tokens"] += r.get("completion_tokens", 0)
            row["cost_usd"] += r.get("cost_usd", 0.0)
        out = [{"name": name, **{k: (round(v, 4) if k == "cost_usd" else v)
                                 for k, v in row.items()},
                "cost_cny": round(row["cost_usd"] * usd_cny_rate(), 2)}
               for name, row in agg.items()]
        out.sort(key=lambda x: x["cost_usd"], reverse=True)
        return out

    # ========== 运维 ==========

    def rotate(self, keep_days: int = KEEP_DAYS) -> dict:
        """轮转：删除 keep_days 天前的记录（bootstrap 每日调用）"""
        if not self.file.exists():
            return {"kept": 0, "removed": 0}
        cutoff = (datetime.now(UTC) - timedelta(days=keep_days)).isoformat()
        kept_rows, removed = [], 0
        try:
            with open(self.file, encoding="utf-8") as f:
                for line in f:
                    try:
                        row = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if row.get("ts", "") >= cutoff:
                        kept_rows.append(line.rstrip("\n"))
                    else:
                        removed += 1
            if removed:
                tmp = self.file.with_suffix(".tmp")
                tmp.write_text("\n".join(kept_rows) + ("\n" if kept_rows else ""),
                               encoding="utf-8")
                tmp.replace(self.file)
        except OSError:
            logger.exception("用量账本轮转失败")
        return {"kept": len(kept_rows), "removed": removed}


# ========== 汇率与共享工具 ==========

def usd_cny_rate() -> float:
    """USD→CNY 汇率（INSFLOW_USD_CNY_RATE，默认 7.2；预算与展示口径用）"""
    import os
    try:
        r = float(os.environ.get("INSFLOW_USD_CNY_RATE", "7.2") or 7.2)
    except (TypeError, ValueError):
        return 7.2
    return r if r > 0 else 7.2


def _local_date_of(ts: str) -> str | None:
    """UTC 时间戳 → 本地日期字符串（YYYY-MM-DD）"""
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    import zoneinfo

    from ..core.work_calendar import local_tzname
    return dt.astimezone(zoneinfo.ZoneInfo(local_tzname())).date().isoformat()


def _local_hour_of(ts: str) -> int | None:
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    import zoneinfo

    from ..core.work_calendar import local_tzname
    return dt.astimezone(zoneinfo.ZoneInfo(local_tzname())).hour


# ========== 每日预算（¥5/天，超支邮件告警，管理员可单日加预算）==========

DEFAULT_DAILY_BUDGET_CNY = 5.0


class DailyBudget:
    """每日预算管理（工作区级，存 settings_json.daily_budget）

    - base_cny：每日基础预算（默认 5 元）
    - override：{date, cny} 单日加预算——仅当 override.date == 今天生效，
      第二天自动失效回落（无需清理任务，读取时判定）
    - 口径：LLM 成本（账本）+ 数据源成本（source.collected 事件的 cost 字段）
    - 超支：发一次邮件告警（当日幂等，状态文件防重复打扰），
      并对非重要 LLM 调用 fail-closed（见 llm.py）
    """

    def __init__(self, workspace_id: str):
        self.workspace_id = workspace_id or "default"

    # ---- 读取/设置 ----

    async def _settings(self) -> dict:
        from ..core.store import get_store
        store = await get_store()
        ws = await store.get_workspace(self.workspace_id)
        return (ws.settings_json or {}) if ws else {}

    async def base_budget_cny(self) -> float:
        cfg = (await self._settings()).get("daily_budget") or {}
        try:
            return float(cfg.get("cny", DEFAULT_DAILY_BUDGET_CNY))
        except (TypeError, ValueError):
            return DEFAULT_DAILY_BUDGET_CNY

    async def set_base_budget_cny(self, cny: float) -> float:
        """修改每日基础预算（控制台设置用）"""
        from ..core.store import get_store
        store = await get_store()
        ws = await store.get_workspace(self.workspace_id)
        if not ws:
            raise ValueError(f"Workspace 不存在: {self.workspace_id}")
        settings = dict(ws.settings_json or {})
        cfg = dict(settings.get("daily_budget") or {})
        cfg["cny"] = round(float(cny), 2)
        settings["daily_budget"] = cfg
        ws.settings_json = settings
        await store.update_workspace(ws)
        self._status_cache.pop(self.workspace_id, None)
        return cfg["cny"]

    async def add_one_day_budget(self, cny: float) -> dict:
        """单日加预算（今天生效，第二天自动回调）"""
        from ..core.store import get_store
        store = await get_store()
        ws = await store.get_workspace(self.workspace_id)
        if not ws:
            raise ValueError(f"Workspace 不存在: {self.workspace_id}")
        settings = dict(ws.settings_json or {})
        cfg = dict(settings.get("daily_budget") or {})
        extra = {"date": local_date().isoformat(), "cny": round(float(cny), 2)}
        cfg["override"] = extra
        settings["daily_budget"] = cfg
        ws.settings_json = settings
        await store.update_workspace(ws)
        self._status_cache.pop(self.workspace_id, None)
        return {"base_cny": cfg.get("cny", DEFAULT_DAILY_BUDGET_CNY), **extra}

    async def effective_budget_cny(self, d=None) -> float:
        """今日生效预算：单日加预算仅在当天生效（次日自动回落基础值）"""
        d = d or local_date()
        cfg = (await self._settings()).get("daily_budget") or {}
        override = cfg.get("override") or {}
        if override.get("date") == d.isoformat():
            try:
                return float(override.get("cny", cfg.get("cny", DEFAULT_DAILY_BUDGET_CNY)))
            except (TypeError, ValueError):
                pass
        try:
            return float(cfg.get("cny", DEFAULT_DAILY_BUDGET_CNY))
        except (TypeError, ValueError):
            return DEFAULT_DAILY_BUDGET_CNY

    # ---- 今日已花 ----

    async def spent_today(self) -> dict:
        """今日成本 = LLM（账本）+ 数据源（事件流 cost）"""
        ledger = UsageLedger(self.workspace_id)
        llm = ledger.today()
        source_cost = await self._source_cost_today()
        total_usd = round(llm.get("cost_usd", 0.0) + source_cost, 4)
        rate = usd_cny_rate()
        return {
            "llm_cost_usd": llm.get("cost_usd", 0.0),
            "source_cost_usd": round(source_cost, 4),
            "total_cost_usd": total_usd,
            "total_cost_cny": round(total_usd * rate, 2),
            "prompt_tokens": llm.get("prompt_tokens", 0),
            "completion_tokens": llm.get("completion_tokens", 0),
            "calls": llm.get("calls", 0),
        }

    async def _source_cost_today(self) -> float:
        from ..core.files import EventBus
        events = EventBus(self.workspace_id).read(limit=1000)
        today = local_date().isoformat()
        total = 0.0
        for e in events:
            if e.get("type") != "source.collected":
                continue
            if _local_date_of(e.get("ts", "")) != today:
                continue
            cost = (e.get("payload") or {}).get("cost", 0)
            if isinstance(cost, (int, float)):
                total += float(cost)
        return total

    # ---- 状态判定 + 超支告警 ----

    # 状态短缓存（LLM 每次调用都要查预算，扫事件流+账本太重；60s 足够新鲜）
    _STATUS_TTL = 60.0
    _status_cache: dict[str, tuple[float, dict]] = {}

    async def status(self, refresh: bool = False) -> dict:
        """预算状态（运营页/日报/熔断共用；60s 缓存，refresh=True 强制重算）"""
        import time
        cached = self._status_cache.get(self.workspace_id)
        if not refresh and cached and time.monotonic() - cached[0] < self._STATUS_TTL:
            return cached[1]
        st = await self._status_uncached()
        self._status_cache[self.workspace_id] = (time.monotonic(), st)
        return st

    async def _status_uncached(self) -> dict:
        spent = await self.spent_today()
        budget_cny = await self.effective_budget_cny()
        budget_usd = budget_cny / usd_cny_rate() if usd_cny_rate() else 0.0
        used_usd = spent["total_cost_usd"]
        ratio = round(used_usd / budget_usd, 3) if budget_usd > 0 else 0.0
        return {
            "budget_cny": round(budget_cny, 2),
            "budget_usd": round(budget_usd, 4),
            "spent": spent,
            "used_ratio": ratio,
            "exceeded": used_usd >= budget_usd > 0,
            "warning": ratio >= 0.8,
            "checked_at": datetime.now(UTC).isoformat(),
        }

    async def exceeded(self) -> bool:
        return (await self.status())["exceeded"]

    async def alert_if_exceeded(self) -> dict | None:
        """超预算 → 邮件告知管理员（当日幂等；未超支返回 None）

        状态文件：data/usage/<ws>/budget_alert_<date>.json（文件优先，重启不重发）
        """
        st = await self.status()
        if not st["exceeded"]:
            return None
        flag = self.usage_dir / f"budget_alert_{local_date().isoformat()}.json"
        if flag.exists():
            return None
        spent = st["spent"]
        body = "\n".join([
            f"今日成本已超出每日预算 {st['budget_cny']} 元。",
            "",
            f"- LLM 成本：${spent['llm_cost_usd']}（调用 {spent['calls']} 次，"
            f"输入 {spent['prompt_tokens']} tokens / 输出 {spent['completion_tokens']} tokens）",
            f"- 数据源成本：${spent['source_cost_usd']}",
            f"- 合计：${spent['total_cost_usd']}（约 ¥{spent['total_cost_cny']}）",
            "",
            "超支期间非重要自动化任务的 LLM 调用已被熔断（重要任务不受影响）。",
            "如需今日继续，请在控制台「自动化运营」单日增加预算（次日自动回调）。",
        ])
        result = await send_admin_email(
            self.workspace_id,
            title=f"每日预算告警：已花 ¥{spent['total_cost_cny']} / 预算 ¥{st['budget_cny']}",
            body=body, severity="high")
        try:
            flag.write_text(json.dumps({"alerted_at": datetime.now(UTC).isoformat(),
                                        "dispatch": result}, ensure_ascii=False),
                            encoding="utf-8")
        except OSError:
            logger.exception("预算告警状态文件写入失败")
        from ..core.files import EventBus
        EventBus(self.workspace_id).emit("budget.daily_exceeded", {
            "budget_cny": st["budget_cny"], "spent_cny": spent["total_cost_cny"],
        })
        return {"dispatch": result, **st}

    @property
    def usage_dir(self) -> Path:
        return UsageLedger(self.workspace_id).usage_dir


def admin_email() -> str:
    """管理员邮箱（环境变量 INSFLOW_ADMIN_EMAIL；邮件日报/预算告警收件人兜底）"""
    import os
    return os.environ.get("INSFLOW_ADMIN_EMAIL", "").strip()


def openflow_admin_email() -> str:
    """从 OpenFlow 侧配置回落管理员邮箱（可选）

    OpenFlow 已有完整的邮件/AI 预算配置（admin/mail-settings.php +
    ai_budget.alert_email → admin_email）。本系统与 OpenFlow 互通只走公开 API，
    不共享业务数据；但收件人属于运维配置，读取是安全的。
    仅当设置 INSFLOW_OPENFLOW_DATA_DIR（指向 OpenFlow data/ 目录）才启用，
    优先级：工作区 alert_targets.email > INSFLOW_ADMIN_EMAIL > OpenFlow alert_email。
    """
    import os
    from pathlib import Path
    base = os.environ.get("INSFLOW_OPENFLOW_DATA_DIR", "").strip()
    if not base:
        return ""
    p = Path(base) / "settings.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        budget = data.get("ai_budget") or {}
        email = (budget.get("alert_email") or data.get("admin_email") or "").strip()
        return email if "@" in email else ""
    except (OSError, ValueError, TypeError, AttributeError):
        return ""


async def resolve_admin_email(workspace_id: str) -> str:
    """管理员邮箱：工作区设置 alert_targets.email.email_to 优先，
    环境变量 / OpenFlow 配置（INSFLOW_OPENFLOW_DATA_DIR 启用时）兜底"""
    from ..core.store import get_store
    try:
        store = await get_store()
        ws = await store.get_workspace(workspace_id)
        target = (((ws.settings_json or {}).get("alert_targets") or {})
                  .get("email") or {}).get("email_to", "") if ws else ""
    except Exception:
        target = ""
    return (target or admin_email() or openflow_admin_email()).strip()


async def send_admin_email(workspace_id: str, *, title: str, body: str,
                           severity: str = "medium") -> dict:
    """给管理员发邮件（复用 email.send 适配器；SMTP 未配置时明确失败不静默）"""
    from ..actions.router import ActionContext, get_action_router
    to = await resolve_admin_email(workspace_id)
    if not to or "@" not in to:
        logger.warning("管理员邮箱未配置（alert_targets.email 或 INSFLOW_ADMIN_EMAIL），跳过发送")
        return {"ok": False, "error": "admin_email_not_configured"}
    return await get_action_router().dispatch(
        {
            "action_type": "email.send",
            "target_ref": to,
            "title": title,
            "summary": body,
            "severity": severity,
        },
        ActionContext(workspace_id=workspace_id, insight_id="admin-email"),
    )
