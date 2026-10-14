"""测试用量账本与每日预算（每日/每小时聚合、消耗大户、单日加预算次日回调）"""

from datetime import UTC, datetime, timedelta

import pytest

import insflow.core.files as files_mod
import insflow.engine.usage_ledger as ul_mod
from insflow.core.entities import Workspace
from insflow.core.store import Store, reset_store
from insflow.engine.usage_ledger import DailyBudget, UsageLedger, usd_cny_rate


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ul_mod, "DATA_DIR", tmp_path)
    monkeypatch.setenv("INSFLOW_USD_CNY_RATE", "7.2")
    DailyBudget._status_cache.clear()

    s = Store(db_path=tmp_path / "test.db")
    await s.connect()
    await s.migrate()
    reset_store(s)
    await s.create_workspace(Workspace(id="test-ws", name="Test WS"))

    yield {"store": s}

    DailyBudget._status_cache.clear()
    reset_store(None)
    await s.close()


def _rec(ledger: UsageLedger, cost: float, model="gpt-4o-mini", task="chat",
         hours_ago: float = 0.0, **kw) -> None:
    row = ledger.record(model=model, task=task, cost_usd=cost,
                        prompt_tokens=100, completion_tokens=50, **kw)
    if hours_ago:
        # 直接改写时间戳模拟历史记录（record 只追加 now）
        import json
        lines = ledger.file.read_text(encoding="utf-8").splitlines()
        old = datetime.now(UTC) - timedelta(hours=hours_ago)
        row_with_ts = {**row, "ts": old.isoformat()}
        lines[-1] = json.dumps(row_with_ts, ensure_ascii=False)
        ledger.file.write_text("\n".join(lines) + "\n", encoding="utf-8")


class TestUsageLedger:
    async def test_record_and_today(self, env):
        ledger = UsageLedger("test-ws")
        _rec(ledger, 0.01)
        _rec(ledger, 0.02)
        today = ledger.today()
        assert today["calls"] == 2
        assert today["prompt_tokens"] == 200
        assert today["completion_tokens"] == 100
        assert abs(today["cost_usd"] - 0.03) < 1e-6

    async def test_daily_includes_zero_days(self, env):
        ledger = UsageLedger("test-ws")
        rows = ledger.daily(days=7)
        assert len(rows) == 7  # 无数据的天也返回 0 行（趋势不断轴）
        assert rows[-1]["date"] == ul_mod.local_date().isoformat()

    async def test_hourly_buckets(self, env):
        ledger = UsageLedger("test-ws")
        _rec(ledger, 0.01, hours_ago=2)
        buckets = ledger.hourly()
        assert sum(b["calls"] for b in buckets) == 1
        assert sum(b["prompt_tokens"] for b in buckets) == 100

    async def test_top_consumers_by_model_and_task(self, env):
        ledger = UsageLedger("test-ws")
        _rec(ledger, 0.10, model="gpt-4o", task="reason")
        _rec(ledger, 0.01, model="gpt-4o-mini", task="chat")
        _rec(ledger, 0.02, model="gpt-4o-mini", task="chat")
        top = ledger.top_consumers(days=7)
        assert top["by_model"][0]["name"] == "gpt-4o"       # 最贵模型排最前
        assert top["by_task"][0]["name"] == "reason"        # 按成本排
        chat_row = next(t for t in top["by_task"] if t["name"] == "chat")
        assert chat_row["calls"] == 2

    async def test_rotate(self, env):
        ledger = UsageLedger("test-ws")
        _rec(ledger, 0.01, hours_ago=24 * 120)  # 120 天前
        _rec(ledger, 0.01)                       # 现在
        out = ledger.rotate(keep_days=90)
        assert out["removed"] == 1 and out["kept"] == 1


class TestDailyBudget:
    async def test_default_budget_is_5_cny(self, env):
        assert await DailyBudget("test-ws").effective_budget_cny() == 5.0

    async def test_admin_email_fallback_to_openflow(self, env, tmp_path, monkeypatch):
        """OpenFlow 配置回落：INSFLOW_OPENFLOW_DATA_DIR 指向其 data/ 目录时，
        读取 ai_budget.alert_email（收件人是运维配置，不属于业务数据）"""
        from insflow.engine.usage_ledger import openflow_admin_email, resolve_admin_email
        of_dir = tmp_path / "of-data"
        of_dir.mkdir()
        (of_dir / "settings.json").write_text(
            '{"ai_budget": {"alert_email": "boss@example.com"}}', encoding="utf-8")
        monkeypatch.setenv("INSFLOW_OPENFLOW_DATA_DIR", str(of_dir))
        assert openflow_admin_email() == "boss@example.com"
        # 工作区自身未配置 → 回落到 OpenFlow
        assert await resolve_admin_email("test-ws") == "boss@example.com"
        # 未启用（未设置环境变量）→ 空不报错
        monkeypatch.delenv("INSFLOW_OPENFLOW_DATA_DIR")
        assert openflow_admin_email() == ""
        # 文件损坏 → 静默回落空
        monkeypatch.setenv("INSFLOW_OPENFLOW_DATA_DIR", str(tmp_path / "nope"))
        assert openflow_admin_email() == ""

    async def test_one_day_override_auto_reverts(self, env):
        budget = DailyBudget("test-ws")
        await budget.add_one_day_budget(20)
        assert await budget.effective_budget_cny() == 20.0  # 今天生效
        # 模拟第二天：override 只认当天日期，次日自动回落基础值（无需清理任务）
        from datetime import date as _date
        tomorrow = _date.fromordinal(ul_mod.local_date().toordinal() + 1)
        assert await budget.effective_budget_cny(tomorrow) == 5.0

    async def test_set_base_budget(self, env):
        budget = DailyBudget("test-ws")
        await budget.set_base_budget_cny(10)
        assert await budget.effective_budget_cny() == 10.0

    async def test_spent_today_and_status(self, env, monkeypatch):
        from insflow.core.files import EventBus
        ledger = UsageLedger("test-ws")
        _rec(ledger, 0.5)
        EventBus("test-ws").emit("source.collected",
                                 {"source": "serper", "kind": "search", "cost": 0.1})
        st = await DailyBudget("test-ws").status(refresh=True)
        assert abs(st["spent"]["llm_cost_usd"] - 0.5) < 1e-3
        assert abs(st["spent"]["source_cost_usd"] - 0.1) < 1e-3
        # ¥5 / 7.2 ≈ $0.694，花了 $0.6 → 未超支但已超 80% 预警线
        assert st["exceeded"] is False
        assert st["warning"] is True

    async def test_exceeded_and_alert_idempotent(self, env, monkeypatch):
        from insflow.core.files import EventBus
        ledger = UsageLedger("test-ws")
        _rec(ledger, 2.0)  # $2 > ¥5/7.2 ≈ $0.694 → 超支
        budget = DailyBudget("test-ws")

        st = await budget.status(refresh=True)
        assert st["exceeded"] is True

        out1 = await budget.alert_if_exceeded()
        assert out1 is not None          # 第一次：发送 + 落状态文件
        assert (budget.usage_dir / f"budget_alert_{ul_mod.local_date().isoformat()}.json").exists()
        out2 = await budget.alert_if_exceeded()
        assert out2 is None              # 第二次：当日幂等，不重发
        events = EventBus("test-ws").read(event_type="budget.daily_exceeded")
        assert len(events) == 1

    async def test_usd_cny_rate_env(self, monkeypatch):
        monkeypatch.setenv("INSFLOW_USD_CNY_RATE", "7.0")
        assert usd_cny_rate() == 7.0
        monkeypatch.setenv("INSFLOW_USD_CNY_RATE", "abc")
        assert usd_cny_rate() == 7.2     # 非法值回落默认
