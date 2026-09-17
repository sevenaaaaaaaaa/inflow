"""表达式级行级权限（RLS policy 表达式 → 参数化 SQL）

目标：viewer/analyst 的可见行不只按"主体白名单"，还能按**表达式策略**限制，
例如 `channel = 'search' and province in ('广东','北京')`。

安全设计（fail-closed）：
- 只允许白名单列 + 白名单运算符；解析失败 → 一律拒绝（`AND 1=0`），不"放过"
- 值一律参数化（不拼接），字符串长度截断，列表长度上限 50
- 变量占位符 `{user.email}` / `{user.role}` 由会话注入（不是用户输入）
- 多条策略之间是 OR（并集）；与 workspace 隔离、主体白名单是 AND（交集）

支持语法：= != > < >= <= in (...) like，and/or 与括号。
"""

import re

COLUMNS = {
    "entity_id": "entity_id",
    "entity_type": "entity_type",
    "metric": "metric",
    "value": "value",
    "day": "day",
    "channel": "channel",
    "province": "province",
    "country": "country",
    "device": "device",
    "campaign": "campaign",
    "step_name": "step_name",
    "source": "source",
    "keyword": "keyword",
    "query": "query",
    "competitor": "competitor",
}
JSON_COLUMNS = {"channel", "province", "country", "device", "campaign", "step_name",
                "source", "keyword", "query", "competitor"}
OPS = {"=", "!=", ">", "<", ">=", "<="}

DENY_ALL = (" AND 1 = 0", [])

TOKEN_RE = re.compile(r"""
    (?P<ws>\s+)
  | (?P<lpar>\()
  | (?P<rpar>\))
  | (?P<op><=|>=|!=|=|<|>)
  | (?P<and>and|AND)
  | (?P<or>or|OR)
  | (?P<not>not|NOT)
  | (?P<in>in|IN)
  | (?P<like>like|LIKE)
  | (?P<num>-?\d+(?:\.\d+)?)
  | (?P<str>'[^']*')
  | (?P<comma>,)
  | (?P<col>[a-zA-Z_][a-zA-Z0-9_]*)
""", re.VERBOSE)


class PolicyError(Exception):
    pass


def _tokenize(expr: str) -> list[tuple[str, str]]:
    pos, out = 0, []
    while pos < len(expr):
        m = TOKEN_RE.match(expr, pos)
        if not m:
            raise PolicyError(f"无法解析：{expr[pos:pos + 12]!r}")
        pos = m.end()
        kind = m.lastgroup
        if kind == "ws":
            continue
        out.append((kind, m.group()))
    return out


def _value(token: tuple[str, str], user: dict) -> tuple[str, object]:
    kind, text = token
    if kind == "str":
        raw = text[1:-1]
        # 变量占位符：{user.email} / {user.role}（来自会话，非用户输入）
        for key in ("email", "role", "workspace_id", "user_id"):
            raw = raw.replace("{" + f"user.{key}" + "}", str(user.get(key) or ""))
        return "?", raw[:200]
    if kind == "num":
        return "?", float(text)
    if kind == "col":
        # 允许 value = 另一列（如 source = channel）——仍受白名单约束
        lowered = text.lower()
        if lowered in ("true", "false"):
            return "?", 1 if lowered == "true" else 0
        if lowered not in COLUMNS:
            raise PolicyError(f"未知列：{text}")
        return COLUMNS[lowered], None
    raise PolicyError(f"非法取值：{text}")


def _column_sql(name: str, dialect=None) -> str:
    """维度列走 json_extract（与查询侧一致）"""
    if name in JSON_COLUMNS:
        if dialect is not None:
            return dialect.json_field("dim_json", name)
        return f"json_extract(dim_json, '$.{name}')"
    return COLUMNS[name]


def compile_policy(expr: str, user: dict | None = None, *,
                   dialect=None) -> tuple[str, list]:
    """策略表达式 → (SQL 条件体, 参数)。任何解析问题都抛 PolicyError（调用方拒绝）"""
    tokens = _tokenize(expr or "")
    if not tokens:
        raise PolicyError("空表达式")
    params: list = []
    out: list[str] = []
    depth = 0
    i = 0
    while i < len(tokens):
        kind, text = tokens[i]
        lowered = text.lower()
        if kind == "lpar":
            out.append("(")
            depth += 1
            i += 1
            continue
        if kind == "rpar":
            out.append(")")
            depth -= 1
            i += 1
            continue
        if kind in ("and", "or"):
            out.append(lowered.upper())
            i += 1
            continue
        if kind == "not":
            out.append("NOT")
            i += 1
            continue
        if kind == "comma":
            i += 1                      # in (...) 内的分隔符
            continue
        if kind == "col":
            if lowered not in COLUMNS:
                raise PolicyError(f"未知列：{text}")
            left = _column_sql(lowered, dialect)
            nxt = tokens[i + 1] if i + 1 < len(tokens) else None
            if nxt and nxt[0] in ("in", "like"):
                if nxt[0] == "in":
                    j = i + 2
                    if j >= len(tokens) or tokens[j][0] != "lpar":
                        raise PolicyError("in 后需要 (值列表)")
                    values = []
                    j += 1
                    while j < len(tokens) and tokens[j][0] != "rpar":
                        if tokens[j][0] == "str" or tokens[j][0] == "num":
                            ph, value = _value(tokens[j], user or {})
                            values.append(value)
                        elif tokens[j][0] != "comma":
                            raise PolicyError("in 列表只允许字面量")
                        j += 1
                    if not values or len(values) > 50:
                        raise PolicyError("in 列表为空或过长")
                    out.append(f"{left} IN (" + ", ".join(["?"] * len(values)) + ")")
                    params.extend(values)
                    i = j + 1
                    continue
                # like：仅允许字符串字面量，且禁止以 % 开头（避免全表通配）
                j = i + 2
                if j >= len(tokens) or tokens[j][0] != "str":
                    raise PolicyError("like 后需要字符串")
                pat = tokens[j][1][1:-1]
                if pat.startswith("%"):
                    raise PolicyError("like 不允许以 % 开头")
                out.append(f"{left} LIKE ?")
                params.append(pat[:80])
                i = j + 1
                continue
            if not nxt or nxt[0] != "op":
                raise PolicyError("列后需要比较运算符")
            right = tokens[i + 2] if i + 2 < len(tokens) else None
            if right is None:
                raise PolicyError("运算符后缺少值")
            ph, value = _value(right, user or {})
            if value is None:           # 右侧是列名
                out.append(f"{left} {nxt[1]} {ph}")
            else:
                out.append(f"{left} {nxt[1]} {ph}")
                params.append(value)
            i += 3
            continue
        if kind in ("str", "num"):
            raise PolicyError("不支持裸字面量")
        raise PolicyError(f"不支持的元素：{text}")
    if depth != 0:
        raise PolicyError("括号不匹配")
    return " ".join(out), params


def policies_for(settings: dict | None, role: str) -> list[str]:
    """该角色的策略表达式列表（空 = 无策略，即不限制）"""
    policies = dict((settings or {}).get("rls_policies") or {})
    raw = policies.get((role or "viewer").lower()) or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(e) for e in raw if str(e).strip()][:5]


def build_policy(settings: dict | None, role: str, user: dict | None = None,
                 *, dialect=None) -> tuple[str, list]:
    """多条策略 OR；任一条解析失败 → 整体拒绝（AND 1=0），绝不"放过" """
    exprs = policies_for(settings, role)
    if not exprs:
        return "", []
    parts, params = [], []
    for expr in exprs:
        try:
            sql, ps = compile_policy(expr, user, dialect=dialect)
        except PolicyError:
            return DENY_ALL
        parts.append(f"({sql})")
        params.extend(ps)
    return " AND (" + " OR ".join(f"({p})" for p in parts) + ")", params
