"""Insight Agent v1

ask 问答：工具调用（query_insights / get_insight_detail / ...）+ 引用溯源。
- LLM 已配置：OpenAI function calling 多轮循环（上限 4 轮），预算账本兜底
- LLM 未配置：retrieval-only 确定性回退（检索 + Skill 匹配 + 洞察摘要），不产生费用
"""

import json
import re

from .llm import LLMGateway
from .skills_host import Skill, get_skills_host
from .tools import AGENT_TOOLS, execute_tool

SYSTEM_PROMPT = """你是 Insight Flow 的增长数据分析师 Agent。

规则：
1. 回答必须基于工具返回的数据，禁止编造数字或结论
2. 引用洞察时必须标注 [ins:洞察ID]，用户可据此溯源
3. 每个结论注明置信度与严重程度（如数据不足则明说）
4. 需要执行动作时用 propose_action 起草**待审批**动作（必须带洞察 ID 与理由），
   并明确告诉用户"需要人工批准后才派发"；不要声称已执行
5. 用户表达长期偏好/重要背景时可 save_note 记住；用户说"以后每天/每周盯一下"
   时用 schedule_task 建定时任务
6. 用户用中文提问时用中文回答
"""

MAX_TOOL_ROUNDS = 4
MAX_TRACKED_PROPOSALS = 3


class InsightAgent:
    """数据洞察问答 Agent"""

    def __init__(self, workspace_id: str, llm: LLMGateway | None = None):
        self.workspace_id = workspace_id
        self.llm = llm or LLMGateway(workspace_id=workspace_id)
        self.skills = get_skills_host()
        self.proposed_actions: list[dict] = []

    # ========== 工具执行 ==========

    async def run_tool(self, name: str, args: dict) -> str:
        """执行 Agent 工具（按 function-calling schema 自动注入 workspace_id）

        之前用硬编码白名单，新增工具（metric_qa/narrate/data_quality/attribution/
        action_lift）漏加 → 调用缺参静默失败、问数退化成洞察检索。改为查 schema 的
        required 字段，新增工具不会再有这个坑。
        """
        args = dict(args or {})
        try:
            from .tools import AGENT_TOOLS
            spec = next((t["function"] for t in AGENT_TOOLS
                         if t["function"]["name"] == name), None)
            required = (spec or {}).get("parameters", {}).get("required", [])
        except Exception:
            required = []
        if "workspace_id" in required and "workspace_id" not in args:
            args["workspace_id"] = self.workspace_id
        output = await execute_tool(name, args)
        if name == "propose_action":
            try:
                data = json.loads(output)
                if (data.get("ok") and data.get("action_id")
                        and len(self.proposed_actions) < MAX_TRACKED_PROPOSALS):
                    self.proposed_actions.append({
                        "action_id": data["action_id"],
                        "action_type": data.get("action_type", ""),
                        "insight_id": data.get("insight_id", ""),
                        "title": data.get("title", ""),
                        "state": data.get("state", "pending"),
                    })
            except (json.JSONDecodeError, TypeError):
                pass
        return output

    # ========== ask 主入口 ==========

    async def ask(self, question: str) -> dict:
        """问答：返回 {answer, citations, skills_used, mode, proposed_actions}

        citations: [{"insight_id": str, "title": str}] —— 引用溯源清单
        proposed_actions: Agent 起草的待审批动作（人在 UI 里批准）
        mode: "llm" | "retrieval"
        """
        matched_skills = self.skills.match(question)
        citations: list[dict] = []
        self.proposed_actions = []

        if self.llm.available:
            try:
                result = await self._ask_with_llm(question, matched_skills, citations)
                mode = "llm"
            except RuntimeError as e:
                # LLM 失败（未配置/预算耗尽/网络）→ 确定性回退，不中断服务
                result = await self._ask_retrieval_only(question, matched_skills, citations)
                result["answer"] += f"\n\n> ⚠️ LLM 推理不可用（{e}），已回退到检索模式。"
                mode = "retrieval"
        else:
            result = await self._ask_retrieval_only(question, matched_skills, citations)
            mode = "retrieval"

        # 收集回答中引用的 insight id（[ins:xxx] 格式）
        cited_ids = set(re.findall(r"\[ins:([\w-]+)\]", result["answer"]))
        if not cited_ids:
            cited_ids = {c["insight_id"] for c in citations}
        result["citations"] = await self._expand_citations(cited_ids)
        result["skills_used"] = [s.name for s in matched_skills]
        result["proposed_actions"] = list(self.proposed_actions)
        result["mode"] = mode
        return result

    # ========== LLM 模式（function calling 循环）==========

    async def _ask_with_llm(self, question: str, skills: list[Skill], citations: list) -> dict:
        memory = await self._memory_context(question)
        messages: list[dict] = [
            {"role": "system", "content": self._system_prompt_with_skills(skills, memory)},
            {"role": "user", "content": question},
        ]

        for _ in range(MAX_TOOL_ROUNDS):
            resp = await self.llm.chat(messages, tools=AGENT_TOOLS)
            tool_calls = resp.get("tool_calls") or []
            if not tool_calls:
                return {"answer": resp.get("content") or ""}

            messages.append(resp.get("raw_message") or {
                "role": "assistant",
                "content": resp.get("content") or "",
                "tool_calls": tool_calls,
            })
            for call in tool_calls:
                fn = call.get("function", {})
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                output = await self.run_tool(name, args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": output,
                })

        # 轮数用尽，最后再取一次无工具总结
        resp = await self.llm.chat(messages)
        return {"answer": resp.get("content") or ""}

    # ========== retrieval-only 模式（无 LLM 确定性回退）==========

    async def _ask_retrieval_only(self, question: str, skills: list[Skill], citations: list) -> dict:
        """检索式回答：查洞察流 → 摘要 + 引用

        这是 Agent 的"降级可用"保障：没有 LLM 也能回答"最近有什么洞察"。
        """
        # 先尝试"问数"（指标/数据质量/归因/增量），命中即答；否则退回洞察检索
        try:
            qa = json.loads(await self.run_tool("metric_qa", {"question": question}))
            if (qa.get("intent") != "value"
                    or qa.get("facts", {}).get("metric")) \
                    and qa.get("answer") and "没听懂" not in qa["answer"]:
                return {"answer": qa["answer"],
                        "citations": qa.get("citations", [])}
        except Exception:
            pass
        tool_output = await self.run_tool("query_insights", {"limit": 10})
        data = json.loads(tool_output)
        insights = data.get("insights", [])

        if not insights:
            return {"answer": (
                "当前工作区暂无洞察。建议先运行一次诊断："
                "`POST /api/v1/diagnosis/run`，或配置上游数据源后等待调度采集。"
            )}

        lines = [f"根据检索（共 {data.get('total', len(insights))} 条最新洞察）：", ""]
        for i, ins in enumerate(insights, 1):
            lines.append(
                f"{i}. [{ins['severity'].upper()}] {ins['title']}"
                f"（置信度 {ins['confidence']:.0%}，状态 {ins['status']}）"
                f" [ins:{ins['id']}]"
            )
            lines.append(f"   {ins['summary'][:120]}")
        if skills:
            skill = skills[0]
            lines.append("")
            lines.append(f"—— 已按「{skill.name}」方法论组织答案；配置 OPENAI_API_KEY 后可启用完整推理问答。")

        return {"answer": "\n".join(lines)}

    # ========== 辅助 ==========

    async def _memory_context(self, question: str) -> str:
        """工作区记忆注入（本地检索，不出工作区）"""
        from ..engine.agent_memory import memory_context
        return await memory_context(self.workspace_id, question)

    def _system_prompt_with_skills(self, skills: list[Skill],
                                   memory: str = "") -> str:
        prompt = SYSTEM_PROMPT
        if memory:
            prompt += "\n\n" + memory
        if skills:
            prompt += "\n\n可用的分析方法论（按需遵循）：\n\n"
            for skill in skills[:2]:
                prompt += skill.to_system_prompt() + "\n\n"
        return prompt

    async def _expand_citations(self, cited_ids: set[str]) -> list[dict]:
        """把 insight id 展开为可溯源的引用条目"""
        from ..core.store import get_store
        store = await get_store()
        citations = []
        for insight_id in sorted(cited_ids)[:20]:
            ins = await store.get_insight(insight_id)
            if ins:
                citations.append({
                    "insight_id": ins.id,
                    "title": ins.title,
                    "severity": ins.severity.value,
                    "type": ins.type,
                })
        return citations
