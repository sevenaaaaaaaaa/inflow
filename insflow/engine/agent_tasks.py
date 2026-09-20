"""Agent 定时任务：把一次问答固化为 cron（每日/每周跑并推送）

- 定义存 agent_tasks 表；调度用 APScheduler（job id = agent.task.<task_id>）
- 执行：InsightAgent.ask(question) → 结果按通道推送（飞书/Webhook/邮件）
- 多实例：handler 由 bootstrap 统一包 leader（与其它定时任务一致）
"""

import logging

logger = logging.getLogger("insflow.agent_tasks")

JOB_PREFIX = "agent.task."
TASK_TYPE = "agent.task.run"
DEFAULT_CHANNELS = ["feishu", "webhook"]
PRESET_CRONS = {"hourly": "0 * * * *", "daily": "0 9 * * *", "weekly": "30 9 * * 1"}


class AgentTaskError(Exception):
    pass


def normalize_cron(cron: str) -> str:
    """预设（hourly/daily/weekly）或 5 段 crontab（UTC）"""
    cron = (cron or "").strip()
    cron = PRESET_CRONS.get(cron, cron)
    try:
        from apscheduler.triggers.cron import CronTrigger
        CronTrigger.from_crontab(cron, timezone="UTC")
    except Exception as e:
        raise AgentTaskError(f"cron 不合法（预设 hourly/daily/weekly 或 5 段式）: {cron}") from e
    return cron


def register_task_job(task: dict) -> bool:
    """注册/更新调度 job；调度器不可用时返回 False（任务定义仍落库）"""
    if not task.get("enabled"):
        return False
    try:
        from ..core.scheduler import get_scheduler
        get_scheduler("default").add_job(
            f"{JOB_PREFIX}{task['id']}", normalize_cron(task["cron"]), TASK_TYPE,
            {"task_id": task["id"]})
        return True
    except Exception:
        logger.exception(f"注册 Agent 任务失败: {task.get('id')}")
        return False


def unregister_task_job(task_id: str) -> None:
    try:
        from ..core.scheduler import get_scheduler
        get_scheduler("default").remove_job(f"{JOB_PREFIX}{task_id}")
    except Exception:
        pass


async def create_task(workspace_id: str, *, name: str, question: str,
                      cron: str = "daily", channels: list | None = None,
                      created_by: str = "") -> dict:
    from ..core.store import get_store
    question = (question or "").strip()
    if not question:
        raise AgentTaskError("question 不能为空")
    cron = normalize_cron(cron)
    store = await get_store()
    task = await store.create_agent_task(
        workspace_id, name=(name or question[:40]), question=question,
        cron=cron, channels=channels or DEFAULT_CHANNELS, created_by=created_by)
    task["scheduled"] = register_task_job(task)
    return task


async def delete_task(workspace_id: str, task_id: str) -> bool:
    from ..core.store import get_store
    store = await get_store()
    task = await store.get_agent_task(task_id)
    if not task or task["workspace_id"] != workspace_id:
        return False
    unregister_task_job(task_id)
    return await store.delete_agent_task(workspace_id, task_id)


async def set_enabled(workspace_id: str, task_id: str, enabled: bool) -> dict:
    from ..core.store import get_store
    store = await get_store()
    task = await store.get_agent_task(task_id)
    if not task or task["workspace_id"] != workspace_id:
        raise AgentTaskError("任务不存在")
    await store.set_agent_task_enabled(workspace_id, task_id, enabled)
    if enabled:
        register_task_job({**task, "enabled": 1})
    else:
        unregister_task_job(task_id)
    return await store.get_agent_task(task_id) or {}


async def run_task(task_id: str) -> dict:
    """执行一次：Ask → 推送 → 记录 last_run（推送失败不影响回答记录）"""
    from ..agent import InsightAgent
    from ..core.store import get_store
    store = await get_store()
    task = await store.get_agent_task(task_id)
    if not task:
        return {"ok": False, "error": f"task not found: {task_id}"}
    workspace_id = task["workspace_id"]
    try:
        result = await InsightAgent(workspace_id).ask(task["question"])
        answer = result.get("answer") or ""
        pushed = await _push(workspace_id, task, answer)
        await store.mark_agent_task_run(task_id, status="ok", summary=answer[:2000])
        return {"ok": True, "task_id": task_id, "mode": result.get("mode"),
                "answer": answer, "citations": result.get("citations") or [],
                "pushed": pushed}
    except Exception as e:
        await store.mark_agent_task_run(task_id, status="error",
                                        summary=f"{type(e).__name__}: {e}"[:2000])
        return {"ok": False, "task_id": task_id, "error": f"{type(e).__name__}: {e}"}


async def _push(workspace_id: str, task: dict, answer: str) -> dict:
    channels = task.get("channels_json") or DEFAULT_CHANNELS
    if not (answer or "").strip():
        return {}
    title = f"[Agent 任务] {task.get('name') or (task.get('question') or '')[:40]}"
    try:
        from .alerts import _notify
        return await _notify(workspace_id, title, answer[:4000], channels, {})
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


async def restore_tasks() -> int:
    """服务启动：把启用的 Agent 任务重新挂到调度器（与 monitor 恢复同思路）"""
    from ..core.store import get_store
    store = await get_store()
    restored = 0
    for ws in await store.list_workspaces():
        for task in await store.list_agent_tasks(ws.id, enabled_only=True):
            if register_task_job(task):
                restored += 1
    return restored


async def run_due_handler(payload: dict | None = None) -> dict:
    """调度 handler 入口（bootstrap 注册；payload={task_id}）"""
    task_id = str((payload or {}).get("task_id") or "")
    if not task_id:
        return {"ok": False, "error": "missing task_id"}
    return await run_task(task_id)
