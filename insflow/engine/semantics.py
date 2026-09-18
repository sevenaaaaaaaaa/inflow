"""语义层：计算字段（派生指标）+ 表计算 + 指标血缘

三层口径（对齐 Metabase 语义层的最小可用集）：
1. **基础指标**：直接来自 metrics 表（如 `ga4_conversions`）
2. **派生指标**（derived）：其他指标的算术组合，登记在 metric_defs.expr
   例：`ga4_conversions / ga4_sessions * 100`（转化率%）
3. **表计算**（table calc / 时序变换）：对任意序列做窗口运算，不落库
   例：`mom`（环比）、`yoy`（同比）、`cum`（累计）、`rolling7`（7 期滚动均值）、
   `share`（占总量比例）

安全：表达式解析用白名单词法（标识符 = 指标名；运算符 + - * / ( )；数字），
**不使用 eval**；指标名必须匹配 `[A-Za-z_][A-Za-z0-9_.]{0,63}`。
血缘：`lineage()` 递归展开 expr 的标识符 → 树（供"这个数从哪来"的追溯）。
"""

import re
from datetime import UTC, datetime, timedelta

IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,63}")
TOKEN = re.compile(r"\s*(?:(?P<num>\d+(?:\.\d+)?)|(?P<ident>[A-Za-z_][A-Za-z0-9_.]{0,63})"
                   r"|(?P<op>[+\-*/()])|(?P<bad>\S))")

TABLE_CALCS = ("none", "mom", "yoy", "cum", "rolling", "share", "diff", "pct_change")


class SemanticError(Exception):
    pass


# ---------------- 表达式解析（无 eval）----------------

def tokenize(expr: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    while pos < len(expr):
        m = TOKEN.match(expr, pos)
        if not m:
            break
        pos = m.end()
        if m.group("bad"):
            raise SemanticError(f"不支持的字符：{m.group('bad')!r}")
        for kind in ("num", "ident", "op"):
            if m.group(kind):
                out.append((kind, m.group(kind)))
    if not out:
        raise SemanticError("表达式为空")
    return out


def _to_rpn(tokens: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """调度场算法（支持一元负号）"""
    prec = {"+": 1, "-": 1, "*": 2, "/": 2}
    out: list[tuple[str, str]] = []
    stack: list[str] = []
    prev: str | None = None
    for kind, val in tokens:
        if kind in ("num", "ident"):
            out.append((kind, val))
        elif val == "(":
            stack.append(val)
        elif val == ")":
            while stack and stack[-1] != "(":
                out.append(("op", stack.pop()))
            if not stack:
                raise SemanticError("括号不匹配")
            stack.pop()
        else:                                  # 运算符
            if val == "-" and (prev is None or prev in ("op", "(")):
                out.append(("num", "0"))       # 一元负号 → 0 - x
            while (stack and stack[-1] != "(" and
                   prec.get(stack[-1], 0) >= prec[val]):
                out.append(("op", stack.pop()))
            stack.append(val)
        prev = val if kind == "op" else (val if val == "(" else "operand")
    while stack:
        op = stack.pop()
        if op == "(":
            raise SemanticError("括号不匹配")
        out.append(("op", op))
    return out


def evaluate(expr: str, values: dict[str, float]) -> float:
    """按给定指标取值计算表达式（缺失指标按 0，除零按 0）"""
    rpn = _to_rpn(tokenize(expr))
    stack: list[float] = []
    for kind, val in rpn:
        if kind == "num":
            stack.append(float(val))
        elif kind == "ident":
            stack.append(float(values.get(val, 0.0) or 0.0))
        else:
            if len(stack) < 2:
                raise SemanticError(f"运算符缺少操作数：{val}")
            b, a = stack.pop(), stack.pop()
            if val == "+":
                stack.append(a + b)
            elif val == "-":
                stack.append(a - b)
            elif val == "*":
                stack.append(a * b)
            else:
                stack.append(a / b if b else 0.0)
    if len(stack) != 1:
        raise SemanticError("表达式不完整")
    return stack[0]


def refs(expr: str) -> list[str]:
    """表达式引用的指标名（血缘用）"""
    return [v for k, v in tokenize(expr) if k == "ident"]


# ---------------- 表计算（时序变换）----------------

def table_calc(values: list[float], mode: str = "none", window: int = 7,
               season: int = 12) -> list[float]:
    """对序列做窗口运算（长度不变，不足处给 0）"""
    vals = [float(v or 0.0) for v in values]
    n = len(vals)
    mode = (mode or "none").lower()
    if mode in ("", "none"):
        return vals
    if mode == "cum":
        out, acc = [], 0.0
        for v in vals:
            acc += v
            out.append(round(acc, 6))
        return out
    if mode == "diff":
        return [0.0] + [round(vals[i] - vals[i - 1], 6) for i in range(1, n)]
    if mode in ("mom", "pct_change"):
        return [0.0] + [round((vals[i] - vals[i - 1]) / vals[i - 1], 6) if vals[i - 1]
                        else 0.0 for i in range(1, n)]
    if mode == "yoy":
        return [round((vals[i] - vals[i - season]) / vals[i - season], 6)
                if i >= season and vals[i - season] else 0.0 for i in range(n)]
    if mode == "rolling":
        w = max(1, min(window, n))
        return [round(sum(vals[max(0, i - w + 1):i + 1]) /
                      len(vals[max(0, i - w + 1):i + 1]), 6) for i in range(n)]
    if mode == "share":
        total = sum(vals) or 1.0
        return [round(v / total, 6) for v in vals]
    raise SemanticError(f"不支持的表计算：{mode}（可用 {TABLE_CALCS}）")


# ---------------- 语义解析与血缘 ----------------

async def resolve_metric(workspace_id: str, name: str, days: float = 30,
                         agg: str = "sum") -> dict:
    """解析一个指标（基础或派生）→ {value, series, definition, lineage}

    派生指标：先解析其引用指标的值，再按 expr 计算（对每个时间点逐点计算）。
    """
    from ..core.store import get_store
    store = await get_store()
    defs = {d["name"]: d for d in await store.list_metric_defs(workspace_id)}
    seen: set[str] = set()

    async def _series(metric: str) -> tuple[list[str], list[float], str]:
        if metric in seen:
            raise SemanticError(f"循环依赖：{metric}")
        seen.add(metric)
        d = defs.get(metric)
        if d and (d.get("expr") or "").strip():
            parts = refs(d["expr"])
            labels, cols = [], {}
            for dep in parts:
                dep_labels, dep_vals, _ = await _series(dep)
                if not labels:
                    labels = dep_labels
                cols[dep] = dep_vals
            n = len(labels)
            vals = [evaluate(d["expr"], {k: (v[i] if i < len(v) else 0.0)
                                        for k, v in cols.items()})
                    for i in range(n)]
            seen.discard(metric)
            return labels, vals, "derived"
        rows = await store.metric_series(workspace_id, metric, days=days, agg=agg)
        seen.discard(metric)
        return ([r["bucket"] for r in rows], [r["value"] for r in rows], "base")

    labels, vals, kind = await _series(name)
    series = [{"bucket": b, "value": v} for b, v in zip(labels, vals)]
    total = evaluate(defs[name]["expr"], {}) if False else (sum(vals) if vals else 0.0)
    # 比率型派生指标用「最新值」更有意义；其余用合计
    d = defs.get(name)
    if kind == "derived" and d and "/" in (d.get("expr") or ""):
        total = vals[-1] if vals else 0.0
    return {"metric": name, "kind": kind, "value": round(total, 6),
            "series": series, "definition": d or {},
            "lineage": await lineage(workspace_id, name)}


async def lineage(workspace_id: str, name: str, depth: int = 0) -> dict:
    """指标血缘：派生指标 → 引用的指标/明细（递归，带环保护）"""
    if depth > 6:
        return {"metric": name, "truncated": True, "depends_on": []}
    from ..core.store import get_store
    store = await get_store()
    defs = {d["name"]: d for d in await store.list_metric_defs(workspace_id)}
    d = defs.get(name)
    if not d or not (d.get("expr") or "").strip():
        return {"metric": name, "kind": "base", "expr": "", "owner": "",
                "unit": "", "version": 1, "depends_on": []}
    children = []
    for dep in refs(d["expr"]):
        children.append(await lineage(workspace_id, dep, depth + 1))
    return {"metric": name, "kind": "derived", "expr": d["expr"],
            "owner": d.get("owner") or "", "unit": d.get("unit") or "",
            "version": d.get("version", 1), "depends_on": children}


async def calc_series(workspace_id: str, metric: str, mode: str = "mom",
                      window: int = 7, days: float = 30, season: int = 12,
                      agg: str = "sum") -> dict:
    """对任意指标（含派生）做表计算 → 序列（不落库）"""
    resolved = await resolve_metric(workspace_id, metric, days=days, agg=agg)
    vals = [p["value"] for p in resolved["series"]]
    labels = [p["bucket"] for p in resolved["series"]]
    out = table_calc(vals, mode, window=window, season=season)
    return {"metric": metric, "mode": mode, "labels": labels,
            "raw": vals, "values": out, "kind": resolved["kind"],
            "window": window}
