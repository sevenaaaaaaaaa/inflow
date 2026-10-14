"""测试运营邮件日报（过去12h 执行/流量/转化 + 当日 token 消耗 + 预算）"""

import pytest

import insflow.core.files as files_mod
import insflow.engine.usage_ledger as ul_mod
from insflow.core.entities import Workspace
from insflow.core.files import EventBus
from insflow.core.store import Store, reset_store
from insflow.engine.email_digest import OpsDigestBuilder
from insflow.engine.usage_ledger import DailyBudget


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ul_mod, "DATA_DIR", tmp_path)
    DailyBudget._status_cache.clear()
    monkeypatch.setenv("INSFLOW_ADMIN_EMAIL", "admin@example.com")

    s = Store(db_path=tmp_path / "test.db")
    await s.connect()
    await s.migrate()
    reset_store(s)
    await s.create_workspace(Workspace(id="test-ws", name="Test WS"))

    yield {"store": s}

    DailyBudget._status_cache.clear()
    reset_store(None)
    await s.close()


async def test_build_empty_workspace(env):
    """空工作区也能出报告（各栏为 0 / 无数据，不抛错）"""
    built = await OpsDigestBuilder("test-ws").build(window_hours=12)
    md = built["markdown"]
    assert "执行情况" in md and "流量情况" in md and "转化情况" in md
    assert "Token 消耗" in md and "预算状态" in md
    summary = built["summary"]
    assert summary["execution"]["monitor_ok"] == 0
    assert summary["execution"]["success_rate"] is None


async def test_build_counts_events(env):
    """过去 12h 的执行/转化事件进入摘要"""
    bus = EventBus("test-ws")
    bus.emit("monitor.run_finished", {"monitor_id": "m1"})
    bus.emit("monitor.run_finished", {"monitor_id": "m1"})
    bus.emit("monitor.alert", {"monitor_id": "m2", "error": "boom"})
    bus.emit("monitor.skipped", {"monitor_id": "m3", "reason": "non-workday"})
    bus.emit("insight.created", {"id": "i1"})
    bus.emit("action.dispatched", {"id": "a1"})
    bus.emit("feedback.received", {"id": "f1"})

    summary = (await OpsDigestBuilder("test-ws").build(window_hours=12))["summary"]
    ex = summary["execution"]
    assert ex["monitor_ok"] == 2 and ex["monitor_fail"] == 1
    assert ex["skipped_non_workday"] == 1
    cv = summary["conversion"]
    assert cv["insights"] == 1 and cv["actions_dispatched"] == 1
    assert cv["verifications"] == 1


async def test_today_includes_usage_and_budget(env):
    """当日 token 消耗与预算状态进入日报"""
    from insflow.engine.usage_ledger import UsageLedger
    UsageLedger("test-ws").record(model="gpt-4o-mini", task="chat",
                                  prompt_tokens=10, completion_tokens=5, cost_usd=0.01)
    built = await OpsDigestBuilder("test-ws").build(window_hours=12)
    today = built["summary"]["today"]
    assert today["usage"]["calls"] == 1
    assert today["budget"]["budget_cny"] == 5.0
    assert "¥" in built["markdown"]
