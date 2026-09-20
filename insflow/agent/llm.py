"""Insight Flow Agent - LLM 网关

OpenAI 兼容网关（多 provider + 预算记账）。
未配置 API Key 时运行在 retrieval-only 模式（确定性回退，不产生费用）。
"""

import os
import time
from dataclasses import dataclass, field

import httpx


@dataclass
class LLMUsage:
    """单次调用记账"""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    model: str = ""


@dataclass
class BudgetLedger:
    """Agent 预算账本（会话级）"""
    calls: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_cost_usd: float = 0.0
    max_cost_usd: float = 5.0
    calls_detail: list[dict] = field(default_factory=list)

    def exceeded(self) -> bool:
        return self.total_cost_usd >= self.max_cost_usd

    def record(self, entry: LLMUsage) -> None:
        self.calls += 1
        self.total_prompt_tokens += entry.prompt_tokens
        self.total_completion_tokens += entry.completion_tokens
        self.total_cost_usd += entry.cost_usd
        self.calls_detail.append({
            "model": entry.model,
            "prompt_tokens": entry.prompt_tokens,
            "completion_tokens": entry.completion_tokens,
            "cost_usd": entry.cost_usd,
            "latency_ms": entry.latency_ms,
        })


class LLMGateway:
    """OpenAI 兼容 Chat Completions 网关

    环境变量：
    - OPENAI_API_KEY / OPENAI_BASE_URL / INSFLOW_AGENT_MODEL
    - INSFLOW_AGENT_BUDGET_USD（会话预算上限，默认 $5）
    未配置时 available=False，Agent 走 retrieval-only 回退。
    """

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None, budget: BudgetLedger | None = None,
                 workspace_id: str = ""):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.model = model or os.environ.get("INSFLOW_AGENT_MODEL", "gpt-4o-mini")
        self.workspace_id = workspace_id or os.environ.get("INSFLOW_WORKSPACE_ID", "")
        self.budget = budget or BudgetLedger(
            max_cost_usd=float(os.environ.get("INSFLOW_AGENT_BUDGET_USD", "5.0"))
        )

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    async def chat(self, messages: list[dict], tools: list[dict] | None = None,
                   temperature: float = 0.3) -> dict:
        """调用 chat/completions（支持 function calling）

        Returns:
            {"content": str|None, "tool_calls": [...], "usage": LLMUsage}
        """
        if not self.available:
            raise RuntimeError("LLM 未配置（OPENAI_API_KEY 缺失）")
        if self.budget.exceeded():
            raise RuntimeError("Agent 会话预算已耗尽（fail-closed）")

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.3,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        start = time.monotonic()
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=60.0,
            )
            resp.raise_for_status()
            data = resp.json()

        entry = LLMUsage(
            prompt_tokens=data.get("usage", {}).get("prompt_tokens", 0),
            completion_tokens=data.get("usage", {}).get("completion_tokens", 0),
            cost_usd=self._estimate_cost(data.get("usage", {})),
            latency_ms=int((time.monotonic() - start) * 1000),
            model=self.model,
        )
        self.budget.record(entry)
        # 成本落库（settings_json.usage.<月>.llm_cost_usd）→ 客户 ROI 跨会话可见
        if self.workspace_id and entry.cost_usd:
            import contextlib
            with contextlib.suppress(Exception):
                from ..engine.billing import BillingManager
                await BillingManager(self.workspace_id).record_usage_persisted(
                    "llm_cost_usd", entry.cost_usd)

        choice = data.get("choices", [{}])[0]
        message = choice.get("message", {})
        return {
            "content": message.get("content"),
            "tool_calls": message.get("tool_calls") or [],
            "usage": entry,
        }

    def _estimate_cost(self, usage: dict) -> float:
        """粗略成本估算（gpt-4o-mini 价：$0.15/M in, $0.60/M out）"""
        pt = usage.get("prompt_tokens", 0)
        ct = usage.get("completion_tokens", 0)
        return pt / 1_000_000 * 0.15 + ct / 1_000_000 * 0.60
