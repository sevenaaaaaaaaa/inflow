"""Insight Flow 通用状态机

对齐 MFlow pipeline_state 模式：
- 显式状态 + 合法迁移表
- 非法迁移抛异常
- 每次迁移写入事件流
"""



class InvalidTransition(Exception):
    """非法状态迁移"""
    pass


class StateMachine:
    """通用状态机

    用法：
        sm = StateMachine(
            states=ActionStates,
            transitions={
                "pending": ["dispatched", "failed"],
                "dispatched": ["done", "failed", "verifying"],
                ...
            },
        )
        sm.validate("pending", "dispatched")  # 合法
        sm.validate("pending", "done")        # 抛 InvalidTransition
    """

    def __init__(self, name: str, transitions: dict[str, list[str]]):
        self.name = name
        self.transitions = transitions

    def validate(self, current: str, target: str) -> bool:
        """校验迁移是否合法"""
        allowed = self.transitions.get(current, [])
        if target not in allowed:
            raise InvalidTransition(
                f"[{self.name}] 非法迁移: {current} → {target}（合法目标: {allowed}）"
            )
        return True

    def can(self, current: str, target: str) -> bool:
        """是否可以迁移"""
        return target in self.transitions.get(current, [])


# ========== 动作验证状态机（架构文档 §7.2）==========
# pending → dispatched → done ─┬─→ verifying → verified(effective|neutral|harmful)
#                              └─→ failed(重试×3 → dead)

ACTION_TRANSITIONS = {
    "pending": ["dispatched", "failed", "cancelled"],
    "dispatched": ["done", "failed"],
    "done": ["verifying", "verified"],
    "verifying": ["verified"],
    "verified": [],
    "failed": ["pending", "dead"],  # 重试回到 pending，重试耗尽进 dead
    "dead": [],
    "cancelled": [],
}

ACTION_MACHINE = StateMachine("action", ACTION_TRANSITIONS)


# ========== 监控任务状态机 ==========
# idle → running → ok | error

MONITOR_TRANSITIONS = {
    "idle": ["running", "paused"],
    "running": ["ok", "error"],
    "ok": ["running", "idle"],
    "error": ["running", "idle"],
    "paused": ["idle"],
}

MONITOR_MACHINE = StateMachine("monitor", MONITOR_TRANSITIONS)


# ========== 洞察状态机 ==========
# new → acknowledged → actioned → verified | dismissed

INSIGHT_TRANSITIONS = {
    "new": ["acknowledged", "dismissed"],
    "acknowledged": ["actioned", "dismissed"],
    "actioned": ["verified", "dismissed"],
    "verified": [],
    "dismissed": [],
}

INSIGHT_MACHINE = StateMachine("insight", INSIGHT_TRANSITIONS)


# ========== 异步任务状态机（深度报告等）==========

TASK_TRANSITIONS = {
    "queued": ["running"],
    "running": ["done", "failed"],
    "done": [],
    "failed": ["queued"],  # 可重试
}

TASK_MACHINE = StateMachine("task", TASK_TRANSITIONS)
