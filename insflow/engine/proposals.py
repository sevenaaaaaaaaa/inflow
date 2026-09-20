"""动作提案（Agent 起草 → 人工审批 → 派发 → 14 天验证）

为什么要有这一层（合规 + 实用）：
1. Agent 只能**起草**（state=pending），不能直接写外部系统；人点批准才走 Action Router
2. 提案必须挂在一条洞察上（引用溯源：为什么现在做这件事）
3. 提案 / 批准 / 拒绝全程写 actions 表 + EventBus，可审计、可追溯
"""

import contextlib

from ..core.entities import Action, ActionState
from ..core.statemachine import ACTION_MACHINE
from ..core.store import get_store

PROPOSE_EVENT = "action.proposed"


class ProposalError(Exception):
    pass


def _action_types() -> set[str]:
    from ..actions.router import get_action_router
    return set(get_action_router().list_types())


async def propose_action(workspace_id: str, *, insight_id: str, action_type: str,
                         target_ref: str = "", params: dict | None = None,
                         rationale: str = "", title: str = "", summary: str = "",
                         severity: str = "medium", confidence: float = 0.5,
                         proposed_by: str = "agent") -> dict:
    """起草待审批动作（不派发；返回 action_id 供人审批）"""
    from ..core.files import EventBus
    store = await get_store()
    insight = await store.get_insight(insight_id) if insight_id else None
    if not insight or insight.workspace_id != workspace_id:
        raise ProposalError(f"洞察不存在或不属于该工作区: {insight_id}")
    if action_type not in _action_types():
        raise ProposalError(f"未注册的动作类型: {action_type}")

    action = await store.create_action(Action(
        workspace_id=workspace_id, insight_id=insight_id,
        action_type=action_type, target_ref=target_ref or "",
        params_json={
            **(params or {}),
            "proposed_by": proposed_by,
            "rationale": (rationale or "")[:2000],
            "title": (title or insight.title)[:512],
            "summary": (summary or insight.summary)[:4000],
            "severity": severity,
            "confidence": confidence,
        },
    ))
    EventBus(workspace_id).emit(PROPOSE_EVENT, {
        "action_id": action.id, "insight_id": insight_id,
        "action_type": action_type, "proposed_by": proposed_by,
    })
    notified = await _notify_proposal(workspace_id, action, insight)
    return {
        "ok": True, "action_id": action.id, "state": "pending",
        "action_type": action_type, "insight_id": insight_id,
        "title": (title or insight.title)[:120],
        "notified": notified,
        "message": "已进入待审批：在控制台「行动验证 / Agent」或通知里批准后才派发",
    }


async def approve_action(workspace_id: str, action_id: str, *,
                         actor: str = "") -> dict:
    """批准并派发：pending → dispatched（+ 基线 + 14 天验证窗口）"""
    from ..actions.feedback_tracker import (
        FeedbackTracker,
        compute_baseline,
    )
    from ..actions.router import ActionContext, get_action_router
    from ..core.files import EventBus
    store = await get_store()
    action = await store.get_action(action_id)
    if not action or action.workspace_id != workspace_id:
        raise ProposalError("动作不存在或不属于该工作区")
    if action.state != ActionState.PENDING:
        raise ProposalError(f"动作当前状态为 {action.state.value}，仅 pending 可批准")

    p = dict(action.params_json or {})
    result = await get_action_router().dispatch(
        {
            "action_type": action.action_type,
            "target_ref": action.target_ref,
            "params_json": p,
            "description": p.get("rationale", ""),
            "title": p.get("title", ""),
            "summary": p.get("summary", ""),
            "severity": p.get("severity", "medium"),
            "confidence": p.get("confidence", 0.5),
        },
        ActionContext(workspace_id=workspace_id, insight_id=action.insight_id),
    )

    tracker = FeedbackTracker(workspace_id)
    if not result.get("ok"):
        failed = await tracker.mark_failed(action, str(result.get("error") or "派发失败"))
        return {"ok": False, "action_id": action_id,
                "error": result.get("error") or "派发失败",
                "state": getattr(failed.state, "value", "failed")}

    insight = await store.get_insight(action.insight_id) if action.insight_id else None
    baseline = await compute_baseline(workspace_id)
    baseline["insight_type"] = getattr(insight, "type", "") or ""
    updated = await tracker.mark_dispatched(action, baseline)
    with contextlib.suppress(Exception):
        await store.update_action_state(action_id, "dispatched", result_json={
            "approved_by": actor, "dispatch": result,
        })
    EventBus(workspace_id).emit("action.approved", {
        "action_id": action_id, "actor": actor, "action_type": action.action_type,
    })
    return {
        "ok": True, "action_id": action_id, "state": "dispatched",
        "verify_window_until": (updated.verify_window_until.isoformat()
                                if getattr(updated, "verify_window_until", None) else ""),
        "baseline": {k: v for k, v in baseline.items() if k != "captured_at"},
    }


async def reject_action(workspace_id: str, action_id: str, *,
                        actor: str = "", reason: str = "") -> dict:
    """拒绝提案：pending → cancelled（保留原因，可审计）"""
    from ..core.files import EventBus
    store = await get_store()
    action = await store.get_action(action_id)
    if not action or action.workspace_id != workspace_id:
        raise ProposalError("动作不存在或不属于该工作区")
    if action.state != ActionState.PENDING:
        raise ProposalError(f"动作当前状态为 {action.state.value}，仅 pending 可拒绝")
    ACTION_MACHINE.validate("pending", "cancelled")
    merged = {**(action.result_json or {}),
              "rejected_by": actor, "reject_reason": (reason or "")[:500]}
    await store.update_action_state(action_id, "cancelled", result_json=merged)
    EventBus(workspace_id).emit("action.rejected", {
        "action_id": action_id, "actor": actor, "reason": reason[:200],
    })
    return {"ok": True, "action_id": action_id, "state": "cancelled"}


async def list_pending(workspace_id: str, limit: int = 50) -> list[dict]:
    """待审批动作（含洞察标题与提案理由，控制台直接渲染）"""
    store = await get_store()
    actions = await store.list_actions(workspace_id, state="pending")
    out = []
    for a in actions[:limit]:
        insight = await store.get_insight(a.insight_id) if a.insight_id else None
        p = dict(a.params_json or {})
        out.append({
            "action_id": a.id,
            "action_type": a.action_type,
            "target_ref": a.target_ref or "",
            "insight_id": a.insight_id,
            "insight_title": getattr(insight, "title", "") or "",
            "proposed_by": p.get("proposed_by", ""),
            "rationale": p.get("rationale", ""),
            "title": p.get("title", ""),
            "summary": p.get("summary", ""),
            "severity": p.get("severity", "medium"),
            "created_at": a.created_at.isoformat() if a.created_at else "",
        })
    return out


async def _notify_proposal(workspace_id: str, action: Action, insight) -> dict:
    """待审批通知（配置了飞书/Webhook 才发；失败不影响提案落库）"""
    try:
        from .alerts import _notify
        p = dict(action.params_json or {})
        title = p.get("title") or getattr(insight, "title", "") or action.action_type
        lines = [f"Agent 起草了一个动作待审批：{action.action_type}"]
        if p.get("rationale"):
            lines.append(f"理由：{p['rationale']}")
        lines.append(f"洞察：{title}")
        lines.append("批准入口：控制台 → 行动验证 → 待审批动作")
        return await _notify(workspace_id, "动作待审批（Agent 提案）",
                             "\n".join(lines), ["feishu", "webhook"], {})
    except Exception as e:                      # 通知失败不改变提案事实
        return {"error": f"{type(e).__name__}: {e}"}
