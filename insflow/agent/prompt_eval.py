"""Prompt 回归评测（golden set）

诚实说明**它能测什么**：
- LLM 未配置（retrieval 模式）：Agent 不走 system prompt，所以这一轮测的是
  **答案管线**（工具选择 / 引用溯源 / 空态措辞）不退化——prompt 改动看不出来，
  报告里 `prompt_sensitive=false` 会标出来
- LLM 已配置：同一组问题跑不同 prompt 版本，pass_rate 可直接对比 → 改 prompt 有回测

用例（prompts/golden/*.jsonl，每行一条）：
  {"id", "question", "modes": ["retrieval","llm"], "expect_any": [...],
   "forbid": [...], "expect_citation": true}
判定：expect_any 命中任意一个 + forbid 一个都不出现 + （可选）有引用。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from .prompts import prompts_dir

DEFAULT_SET = "agent_qa"


def golden_dir() -> Path:
    return prompts_dir() / "golden"


def load_cases(name: str = DEFAULT_SET, path: str | Path = "") -> list[dict]:
    src = Path(path) if path else golden_dir() / f"{name}.jsonl"
    if not src.exists():
        return []
    cases = []
    for line in src.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError:
            continue
        if case.get("question"):
            cases.append(case)
    return cases


def judge(case: dict, answer: str, citations: list) -> dict:
    """单条判定（纯函数，便于单测）"""
    text = answer or ""
    expect = [e for e in (case.get("expect_any") or []) if e]
    hit = next((e for e in expect if e in text), "") if expect else ""
    forbidden = next((f for f in (case.get("forbid") or []) if f and f in text), "")
    need_cite = bool(case.get("expect_citation"))
    has_cite = bool(citations) or "[ins:" in text
    reasons = []
    if expect and not hit:
        reasons.append(f"未命中任一关键词 {expect}")
    if forbidden:
        reasons.append(f"出现禁止表述「{forbidden}」")
    if need_cite and not has_cite:
        reasons.append("缺少洞察引用")
    return {"id": case.get("id", ""), "passed": not reasons, "hit": hit,
            "reasons": reasons}


async def run_eval(workspace_id: str, *, name: str = "agent_system",
                   version: int | None = None, set_name: str = DEFAULT_SET,
                   cases: list[dict] | None = None, write: bool = False) -> dict:
    """跑一遍 golden set；返回 {prompt, mode, total, passed, score, cases}"""
    from . import InsightAgent
    items = cases if cases is not None else load_cases(set_name)
    agent = InsightAgent(workspace_id, prompt_version=version)
    mode = "llm" if agent.llm.available else "retrieval"
    results = []
    for case in items:
        modes = case.get("modes") or ["retrieval", "llm"]
        if mode not in modes:
            results.append({"id": case.get("id", ""), "skipped": True,
                            "reasons": [f"用例只在 {modes} 模式下有效"]})
            continue
        try:
            res = await agent.ask(case["question"])
        except Exception as e:  # noqa: BLE001 —— 评测不该因单条异常中断
            results.append({"id": case.get("id", ""), "passed": False,
                            "reasons": [f"调用异常: {type(e).__name__}: {e}"]})
            continue
        verdict = judge(case, res.get("answer", ""), res.get("citations") or [])
        verdict["mode"] = res.get("mode", mode)
        results.append(verdict)

    graded = [r for r in results if not r.get("skipped")]
    passed = sum(1 for r in graded if r.get("passed"))
    report = {
        "workspace_id": workspace_id,
        "prompt": agent.prompt.ref,
        "prompt_name": name,
        "prompt_version": agent.prompt.version,
        "mode": mode,
        # retrieval 模式不经过 system prompt：分数只代表管线没退化
        "prompt_sensitive": mode == "llm",
        "set": set_name,
        "total": len(graded),
        "skipped": len(results) - len(graded),
        "passed": passed,
        "score": round(passed / len(graded), 3) if graded else 0.0,
        "cases": results,
        "created_at": datetime.now(UTC).isoformat(),
    }
    if write:
        report["path"] = _write_report(report)
    return report


def _write_report(report: dict) -> str:
    from ..core.files import DATA_DIR
    out = Path(DATA_DIR) / "prompt_evals" / report["workspace_id"]
    out.mkdir(parents=True, exist_ok=True)
    stamp = report["created_at"].replace(":", "").replace("-", "")[:15]
    path = out / f"{report['prompt_name']}-v{report['prompt_version']}-{stamp}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


async def compare(workspace_id: str, versions: list[int], *,
                  set_name: str = DEFAULT_SET) -> dict:
    """多版本对照（LLM 未配置时各版本分数必然相同——报告里已标注原因）"""
    runs = [await run_eval(workspace_id, version=v, set_name=set_name)
            for v in versions]
    best = max(runs, key=lambda r: r["score"]) if runs else None
    return {
        "versions": [{"version": r["prompt_version"], "score": r["score"],
                      "passed": r["passed"], "total": r["total"],
                      "prompt": r["prompt"]} for r in runs],
        "prompt_sensitive": bool(runs and runs[0]["prompt_sensitive"]),
        "best_version": best["prompt_version"] if best else None,
        "runs": runs,
    }
