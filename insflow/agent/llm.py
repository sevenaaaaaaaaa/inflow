"""Insight Flow Agent - LLM 网关

OpenAI 兼容网关（多 provider + 预算记账 + 模型路由）。
未配置 API Key 时运行在 retrieval-only 模式（确定性回退，不产生费用）。

模型路由（批次 F）：按**任务类型**分层，抽取/分类/摘要走便宜模型，推理走强模型；
预算用到阈值（默认 70%）后强模型自动降级为便宜模型——预算耗尽前先降质量，
而不是直接 fail-closed 把功能砍掉。价格表可用 `INSFLOW_MODEL_PRICES`（JSON）覆盖。
"""

import json
import os
import time
from dataclasses import dataclass, field

import httpx

# 每百万 token 价格（in, out）——粗略估算，用于预算与 ROI 口径，不是账单
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "o4-mini": (1.10, 4.40),
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
    "qwen-plus": (0.40, 1.20),
    "qwen-turbo": (0.05, 0.20),
    "glm-4-air": (0.10, 0.10),
    "claude-3-5-haiku": (0.80, 4.00),
    "claude-sonnet": (3.00, 15.00),
}
FALLBACK_PRICE = (0.50, 1.50)

# 任务 → 层级：抽取/分类/摘要/工具路由便宜做，推理/对话用强模型
TASK_TIERS = {
    "extract": "cheap", "classify": "cheap", "summarize": "cheap",
    "route": "cheap", "translate": "cheap",
    "reason": "strong", "chat": "strong", "narrate": "strong",
}


def model_prices() -> dict[str, tuple[float, float]]:
    """价格表（INSFLOW_MODEL_PRICES 为 {"model": [in, out]} 的 JSON，可增可改）"""
    prices = dict(DEFAULT_PRICES)
    raw = os.environ.get("INSFLOW_MODEL_PRICES", "").strip()
    if raw:
        try:
            for name, pair in json.loads(raw).items():
                prices[str(name)] = (float(pair[0]), float(pair[1]))
        except (ValueError, TypeError, KeyError, IndexError):
            import logging
            logging.getLogger("insflow.llm").warning("INSFLOW_MODEL_PRICES 解析失败，用默认价格表")
    return prices


def price_of(model: str) -> tuple[float, float]:
    """最长前缀匹配（gpt-4o-mini-2024-07-18 → gpt-4o-mini）"""
    prices = model_prices()
    best = ""
    for name in prices:
        if (model or "").startswith(name) and len(name) > len(best):
            best = name
    return prices[best] if best else FALLBACK_PRICE


@dataclass
class LLMUsage:
    """单次调用记账"""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    model: str = ""
    tier: str = ""
    task: str = ""
    degraded: bool = False


@dataclass
class BudgetLedger:
    """Agent 预算账本（会话级）"""
    calls: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_cost_usd: float = 0.0
    max_cost_usd: float = 5.0
    degraded_calls: int = 0
    calls_detail: list[dict] = field(default_factory=list)

    def exceeded(self) -> bool:
        return self.total_cost_usd >= self.max_cost_usd

    def used_ratio(self) -> float:
        return self.total_cost_usd / self.max_cost_usd if self.max_cost_usd > 0 else 0.0

    def by_model(self) -> dict[str, dict]:
        """按模型汇总（看清钱花在哪一层）"""
        out: dict[str, dict] = {}
        for c in self.calls_detail:
            row = out.setdefault(c.get("model", ""), {
                "calls": 0, "cost_usd": 0.0, "tier": c.get("tier", "")})
            row["calls"] += 1
            row["cost_usd"] = round(row["cost_usd"] + c.get("cost_usd", 0.0), 6)
        return out

    def record(self, entry: LLMUsage) -> None:
        self.calls += 1
        self.total_prompt_tokens += entry.prompt_tokens
        self.total_completion_tokens += entry.completion_tokens
        self.total_cost_usd += entry.cost_usd
        if entry.degraded:
            self.degraded_calls += 1
        self.calls_detail.append({
            "model": entry.model,
            "tier": entry.tier,
            "task": entry.task,
            "degraded": entry.degraded,
            "prompt_tokens": entry.prompt_tokens,
            "completion_tokens": entry.completion_tokens,
            "cost_usd": entry.cost_usd,
            "latency_ms": entry.latency_ms,
        })


class LLMGateway:
    """OpenAI 兼容 Chat Completions 网关

    环境变量：
    - OPENAI_API_KEY / OPENAI_BASE_URL / INSFLOW_AGENT_MODEL
    - INSFLOW_AGENT_MODEL_CHEAP / INSFLOW_AGENT_MODEL_STRONG（模型路由两层，缺省都回落 MODEL）
    - INSFLOW_AGENT_BUDGET_USD（会话预算上限，默认 $5）
    - INSFLOW_AGENT_DEGRADE_AT（预算用到多少比例开始降级，默认 0.7）
    未配置时 available=False，Agent 走 retrieval-only 回退。
    """

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None, budget: BudgetLedger | None = None,
                 workspace_id: str = ""):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.model = model or os.environ.get("INSFLOW_AGENT_MODEL", "gpt-4o-mini")
        self.model_cheap = os.environ.get("INSFLOW_AGENT_MODEL_CHEAP", "") or self.model
        self.model_strong = os.environ.get("INSFLOW_AGENT_MODEL_STRONG", "") or self.model
        try:
            self.degrade_at = float(os.environ.get("INSFLOW_AGENT_DEGRADE_AT", "0.7"))
        except ValueError:
            self.degrade_at = 0.7
        self.workspace_id = workspace_id or os.environ.get("INSFLOW_WORKSPACE_ID", "")
        self.budget = budget or BudgetLedger(
            max_cost_usd=float(os.environ.get("INSFLOW_AGENT_BUDGET_USD", "5.0"))
        )

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    # ---- 模型路由 ----

    def resolve_model(self, task: str = "reason") -> tuple[str, str, bool]:
        """任务 → (模型, 层级, 是否降级)

        降级条件：强模型任务 + 预算已用过阈值 + 确实配了更便宜的模型。
        没配两层模型时路由是恒等映射（行为与之前完全一致，不会偷偷换模型）。
        """
        tier = TASK_TIERS.get(task, "strong")
        model = self.model_strong if tier == "strong" else self.model_cheap
        degraded = False
        if (tier == "strong" and self.model_cheap != self.model_strong
                and self.budget.used_ratio() >= self.degrade_at):
            model, tier, degraded = self.model_cheap, "cheap", True
        return model, tier, degraded

    def routing(self) -> dict:
        """当前路由配置（运维/控制台可见，避免"为什么这次贵了"变成玄学）"""
        return {
            "cheap": self.model_cheap, "strong": self.model_strong,
            "degrade_at": self.degrade_at,
            "budget_usd": self.budget.max_cost_usd,
            "used_usd": round(self.budget.total_cost_usd, 6),
            "used_ratio": round(self.budget.used_ratio(), 3),
            "degraded_calls": self.budget.degraded_calls,
            "task_tiers": dict(TASK_TIERS),
            "prices": {k: list(v) for k, v in model_prices().items()},
        }

    async def chat(self, messages: list[dict], tools: list[dict] | None = None,
                   temperature: float = 0.3, task: str = "reason") -> dict:
        """调用 chat/completions（支持 function calling + 按任务选模型）

        Returns:
            {"content": str|None, "tool_calls": [...], "usage": LLMUsage, "model": str}
        """
        if not self.available:
            raise RuntimeError("LLM 未配置（OPENAI_API_KEY 缺失）")
        if self.budget.exceeded():
            raise RuntimeError("Agent 会话预算已耗尽（fail-closed）")

        model, tier, degraded = self.resolve_model(task)
        payload = {
            "model": model,
            "messages": messages,
            # 之前这里写死 0.3，调用方传的 temperature 被静默吞掉（叙事润色想要更低温度也没用）
            "temperature": temperature,
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
            cost_usd=self._estimate_cost(data.get("usage", {}), model),
            latency_ms=int((time.monotonic() - start) * 1000),
            model=model, tier=tier, task=task, degraded=degraded,
        )
        await self._record(entry)

        choice = data.get("choices", [{}])[0]
        message = choice.get("message", {})
        return {
            "content": message.get("content"),
            "tool_calls": message.get("tool_calls") or [],
            "usage": entry,
            "model": model,
            "tier": tier,
            "degraded": degraded,
        }

    async def _record(self, entry: LLMUsage) -> None:
        """记账：会话账本 + 成本落库（settings_json.usage.<月>.llm_cost_usd）"""
        self.budget.record(entry)
        if self.workspace_id and entry.cost_usd:
            import contextlib
            with contextlib.suppress(Exception):
                from ..engine.billing import BillingManager
                await BillingManager(self.workspace_id).record_usage_persisted(
                    "llm_cost_usd", entry.cost_usd)

    async def chat_stream(self, messages: list[dict], temperature: float = 0.3,
                          task: str = "reason"):
        """真流式（SSE over httpx）：逐块 yield 文本，最后一帧记账

        不支持 tools：function calling 要完整 JSON 才能执行，边流边拼 arguments
        收益为零、出错面翻倍——工具轮仍走非流式 chat()，只有收尾总结用这里。
        """
        if not self.available:
            raise RuntimeError("LLM 未配置（OPENAI_API_KEY 缺失）")
        if self.budget.exceeded():
            raise RuntimeError("Agent 会话预算已耗尽（fail-closed）")

        model, tier, degraded = self.resolve_model(task)
        payload = {"model": model, "messages": messages, "temperature": temperature,
                   "stream": True, "stream_options": {"include_usage": True}}
        start = time.monotonic()
        usage: dict = {}
        out_chars = 0
        async with httpx.AsyncClient() as client, client.stream(
                "POST", f"{self.base_url}/chat/completions", json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=120.0) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if raw == "[DONE]":
                    break
                try:
                    chunk = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if chunk.get("usage"):
                    usage = chunk["usage"]
                for choice in chunk.get("choices") or []:
                    piece = (choice.get("delta") or {}).get("content") or ""
                    if piece:
                        out_chars += len(piece)
                        yield piece
        if not usage:
            # 有的兼容网关不回 usage：按字符粗估（记账宁可偏高，也不要静默漏记成本）
            usage = {
                "prompt_tokens": sum(len(str(m.get("content") or "")) for m in messages) // 3,
                "completion_tokens": out_chars // 3,
            }
        await self._record(LLMUsage(
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            cost_usd=self._estimate_cost(usage, model),
            latency_ms=int((time.monotonic() - start) * 1000),
            model=model, tier=tier, task=task, degraded=degraded))

    def _estimate_cost(self, usage: dict, model: str = "") -> float:
        """粗略成本估算（按模型价格表；未知模型用保守缺省价）"""
        price_in, price_out = price_of(model or self.model)
        pt = usage.get("prompt_tokens", 0)
        ct = usage.get("completion_tokens", 0)
        return pt / 1_000_000 * price_in + ct / 1_000_000 * price_out
