"""Insight Flow 动作验证状态机 + FeedbackTracker

动作验证状态机（架构文档 §7.2）——北极星指标的基础设施：
    pending → dispatched → done ─┬─→ verifying → verified(effective|neutral|harmful)
                                 └─→ failed(重试×3 → dead)

验证逻辑：dispatched 时记录 baseline_json（GSC 点击/GA4 转化/排名基线）
verify_window_until 到期（默认 14 天）→ feedback.evaluate 拉取对比
→ 写 feedback + 更新模型效果分 → 《验证报告》落盘
"""

from datetime import UTC, datetime, timedelta

from ..core.entities import Action, ActionVerdict, Feedback, InsightStatus
from ..core.files import EventBus, ReportStore
from ..core.statemachine import ACTION_MACHINE
from ..core.store import get_store

# 验证窗口（PRD AC-6：默认 14 天）
DEFAULT_VERIFY_WINDOW_DAYS = 14
# 失败重试上限（×3 → dead）
MAX_RETRIES = 3


class VerificationError(Exception):
    pass


class FeedbackTracker:
    """动作验证追踪器

    生命周期钩子：
    - mark_dispatched(action)：派发时记录基线 + 打开验证窗口
    - mark_done / mark_failed：执行结果回填（失败自动重试）
    - start_verification / evaluate：窗口到期后拉取指标对比 → verdict
    """

    def __init__(self, workspace_id: str):
        self.workspace_id = workspace_id
        self.bus = EventBus(workspace_id)

    # ========== 基线记录（dispatched 时调用）==========

    async def mark_dispatched(self, action: Action, baseline: dict | None = None) -> Action:
        """派发动作：记录基线 + 开 14 天验证窗口

        baseline 结构（可由 GSC/GA4 采集器填充）：
        {"gsc_clicks": 120, "ga4_sessions": 3000, "captured_at": "..."}
        """
        ACTION_MACHINE.validate(action.state.value if hasattr(action.state, "value") else action.state,
                                "dispatched")
        now = datetime.now(UTC)
        store = await get_store()
        await store.update_action_state(
            action.id,
            "dispatched",
            dispatched_at=now,
            verify_window_until=now + timedelta(days=DEFAULT_VERIFY_WINDOW_DAYS),
            baseline_json=baseline or {},
        )
        self.bus.emit("action.dispatched", {
            "action_id": action.id,
            "insight_id": action.insight_id,
            "baseline": baseline or {},
            "verify_window_until": (now + timedelta(days=DEFAULT_VERIFY_WINDOW_DAYS)).isoformat(),
        })
        updated = await store.get_action(action.id)
        return updated or action

    async def mark_done(self, action: Action, result: dict) -> Action:
        """执行成功 → done（等待验证窗口）→ 自动进入 verifying"""
        ACTION_MACHINE.validate(action.state.value if hasattr(action.state, "value") else action.state, "done")
        store = await get_store()
        await store.update_action_state(action.id, "done", result_json=result)

        # 有验证窗口 → verifying
        if action.verify_window_until:
            ACTION_MACHINE.validate("done", "verifying")
            await store.update_action_state(action.id, "verifying")

        self.bus.emit("action.executed", {"action_id": action.id, "result": result})
        return await store.get_action(action.id) or action

    async def mark_failed(self, action: Action, error: str) -> Action:
        """执行失败 → 重试（≤3 次回 pending）或 dead"""
        cur_state = action.state.value if hasattr(action.state, "value") else action.state
        if cur_state != "failed":  # 重试时已在 failed，跳过重复校验
            ACTION_MACHINE.validate(cur_state, "failed")
        result = {"error": error}

        # 已有 result_json 里的 retry 计数
        store = await get_store()
        current = await store.get_action(action.id)
        retries = (current.result_json.get("retries", 0) if current else 0) + 1

        if retries >= MAX_RETRIES:
            await store.update_action_state(action.id, "dead", result_json={**result, "retries": retries})
            self.bus.emit("action.dead", {"action_id": action.id, "retries": retries})
        else:
            await store.update_action_state(action.id, "failed", result_json={**result, "retries": retries})
            self.bus.emit("action.failed", {"action_id": action.id, "error": error, "retry": retries})
        return await store.get_action(action.id) or action

    # ========== 验证评估（窗口到期后由调度器触发）==========

    async def evaluate(self, action: Action, current_metrics: dict | None = None) -> Feedback:
        """窗口到期：拉取当前指标 vs 基线 → verdict + feedback + 验证报告

        current_metrics 结构与 baseline 同构：
        {"gsc_clicks": 150, "ga4_sessions": 3600}（不含 captured_at）
        """
        if not action.baseline_json:
            raise VerificationError(f"动作 {action.id} 无基线数据，无法验证")

        store = await get_store()

        # 状态校验：verifying（或 done 未进入 verifying 的补一步）
        cur_state = action.state.value if hasattr(action.state, "value") else action.state
        if cur_state in ("done",):
            ACTION_MACHINE.validate("done", "verifying")
            await store.update_action_state(action.id, "verifying")
        ACTION_MACHINE.validate("verifying", "verified")

        verdicts: list[ActionVerdict] = []
        feedbacks: list[Feedback] = []

        for metric, baseline_value in action.baseline_json.items():
            if not isinstance(baseline_value, (int, float)):
                continue
            after = (current_metrics or {}).get(metric)
            if after is None:
                continue  # 该指标暂无数据，跳过
            delta = after - baseline_value
            # 判定：提升 ≥10% → effective；变化 <10% → neutral；下降 ≥10% → harmful
            pct = delta / baseline_value if baseline_value else 0.0
            if pct >= 0.10:
                verdict = ActionVerdict.EFFECTIVE
            elif pct <= -0.10:
                verdict = ActionVerdict.HARMFUL
            else:
                verdict = ActionVerdict.NEUTRAL
            verdicts.append(verdict)

            fb = await store.create_feedback(Feedback(
                workspace_id=action.workspace_id,
                action_id=action.id,
                metric=metric,
                before=float(baseline_value),
                after=float(after),
                delta=delta,
                verdict=verdict,
            ))
            feedbacks.append(fb)

        # 综合判定：任一 effective → effective；否则任一 harmful → harmful；否则 neutral
        if ActionVerdict.EFFECTIVE in verdicts:
            final = ActionVerdict.EFFECTIVE
        elif ActionVerdict.HARMFUL in verdicts and ActionVerdict.NEUTRAL not in verdicts:
            final = ActionVerdict.HARMFUL
        else:
            final = ActionVerdict.NEUTRAL

        await store.update_action_state(
            action.id, "verified",
            result_json={**(action.result_json or {}), "verdict": final.value},
        )

        # 洞察 → verified（闭环）
        if action.insight_id:
            insight = await store.get_insight(action.insight_id)
            if insight and insight.status == InsightStatus.ACTIONED:
                await store.update_insight_status(action.insight_id, "verified")

        self.bus.emit("feedback.received", {
            "action_id": action.id,
            "verdict": final.value,
            "metrics": len(feedbacks),
        })

        # 《验证报告》落盘
        report_md = self._render_report(action, feedbacks, final, current_metrics or {})
        ReportStore(self.workspace_id).save_report("verification", report_md)

        return feedbacks[0] if feedbacks else Feedback(
            workspace_id=action.workspace_id,
            action_id=action.id,
            metric="none",
            before=0, after=0, delta=0,
            verdict=final,
        )

    def _render_report(
        self,
        action: Action,
        feedbacks: list[Feedback],
        final: ActionVerdict,
        current_metrics: dict | None = None,
    ) -> str:
        now = datetime.now(UTC)
        verdict_cn = {
            ActionVerdict.EFFECTIVE: "✅ 有效",
            ActionVerdict.NEUTRAL: "➖ 中性",
            ActionVerdict.HARMFUL: "❌ 有害",
        }
        lines = [
            "# 选题假设验证报告",
            "",
            "| 项 | 内容 |",
            "|---|---|",
            f"| 动作 ID | {action.id} |",
            f"| 关联洞察 | {action.insight_id} |",
            f"| 动作类型 | {action.action_type} |",
            f"| 派发时间 | {action.dispatched_at} |",
            f"| 验证完成 | {now.strftime('%Y-%m-%d %H:%M UTC')} |",
            f"| 综合结论 | **{verdict_cn[final]}** |",
            "",
            "| 指标 | 基线 | 验证期 | 变化 | 判定 |",
            "|---|---|---|---|---|",
        ]
        for fb in feedbacks:
            pct = fb.delta / fb.before if fb.before else 0
            lines.append(f"| {fb.metric} | {fb.before:.0f} | {fb.after:.0f} | {pct:+.0%} | {verdict_cn[fb.verdict]} |")

        if current_metrics:
            lines += ["", "## 当前完整指标", "", f"```json\n{current_metrics}\n```"]

        lines += [
            "",
            "> 结论反馈给模型效果分：同类洞察的置信度权重将据此调整。",
            "",
        ]
        return "\n".join(lines)

    # ========== 调度入口：批量评估到期动作 ==========

    async def evaluate_due_actions(self, metrics_provider=None) -> list[dict]:
        """调度器调用：扫描所有验证窗口到期的动作并评估

        metrics_provider: async fn(action) -> dict（从 GSC/GA4 拉当前指标）
                          未提供则用 baseline 的 0 值回退评估
        """
        store = await get_store()
        due = await store.list_actions_due_for_verification()
        results = []
        for action in due:
            current = await metrics_provider(action) if metrics_provider else {}
            try:
                await self.evaluate(action, current)
                results.append({"action_id": action.id, "evaluated": True})
            except VerificationError as e:
                results.append({"action_id": action.id, "evaluated": False, "error": str(e)})
        return results
