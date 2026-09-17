"""只读 SQL 沙箱（即席探索）——严格白名单 + 强制租户隔离，fail-closed

约束（宁可少给能力，也不给注入面）：
1. 只允许单条 SELECT（禁 ; 多语句、禁 DDL/DML/PRAGMA/ATTACH/LOAD/VACUUM/OUTFILE）
2. 只允许白名单表
3. **强制租户隔离**：把每个表引用改写成带 `workspace_id = ?` 的子查询，
   用户 SQL 无法绕过（其自身条件只能在已过滤集合上继续收窄）
4. 结果行数硬上限 500，避免拖垮实例

定位：分析同学要临时算个数，不必等开发加接口；但只能在**自己的**数据上只读查询。
"""

import re

FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|truncate|grant|revoke|"
    r"attach|detach|pragma|vacuum|load_extension|into\s+outfile|for\s+update|"
    r"sqlite_master|information_schema|performance_schema|mysql\.)\b",
    re.IGNORECASE)
ALLOWED_TABLES = {"metrics", "metric_daily", "insights", "actions", "competitors",
                  "journey_events", "comments", "monitors", "subscriptions"}
TABLE_RE = re.compile(
    r"\b(from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)"
    r"(\s+(?:as\s+)?([a-zA-Z_][a-zA-Z0-9_]*))?",
    re.IGNORECASE)
# 这些词不是别名（避免把 where/group 等关键字当成别名）
KEYWORDS = {"where", "group", "order", "limit", "having", "on", "join", "left",
            "right", "inner", "outer", "union", "and", "or", "as", "for", "use"}
MAX_ROWS = 500


class SqlError(Exception):
    pass


def validate(sql: str) -> str:
    """校验 SQL（通过则返回规整后的语句）"""
    text = (sql or "").strip()
    if not text:
        raise SqlError("SQL 为空")
    if text.rstrip().endswith(";"):
        text = text.rstrip()[:-1]
    if ";" in text:
        raise SqlError("只允许单条语句（禁止 ; 分隔的多语句）")
    if not re.match(r"^select\b", text, re.IGNORECASE):
        raise SqlError("只允许 SELECT 查询")
    if FORBIDDEN.search(text):
        raise SqlError("包含禁止的关键字（只读沙箱）")
    table_matches = list(TABLE_RE.finditer(text))
    if not table_matches:
        raise SqlError("查询需包含白名单内的表")
    unknown = {m.group(2).lower() for m in table_matches} - ALLOWED_TABLES
    if unknown:
        raise SqlError(f"不允许访问的表：{', '.join(sorted(unknown))}")
    return text


def isolate_tenant(sql: str, workspace_id: str) -> tuple[str, list]:
    """把每个表引用改写成带租户过滤的子查询；返回 (sql, params)"""
    if not workspace_id:
        raise SqlError("缺少 workspace_id")
    params: list = []

    counter = {"i": 0}

    def _sub(match: re.Match) -> str:
        keyword, table = match.group(1), match.group(2)
        alias = (match.group(4) or "").strip()
        params.append(workspace_id)
        if not alias or alias.lower() in KEYWORDS:
            # 未写别名（或被关键字占据）→ 补默认别名：
            # MySQL 要求派生表必须有别名（SQLite 容忍，MySQL 报 1248）
            counter["i"] += 1
            alias_sql = f" AS t{counter['i']} "
            tail = match.group(3) or ""
            # 关键字（如 WHERE）要保留在别名之后
            if alias and alias.lower() in KEYWORDS:
                alias_sql = f" AS t{counter['i']} {alias} "
            elif tail.strip().lower() in KEYWORDS:
                alias_sql = f" AS t{counter['i']} {tail.strip()} "
            return f"{keyword} (SELECT * FROM {table} WHERE workspace_id = ?){alias_sql}"
        return (f"{keyword} (SELECT * FROM {table} WHERE workspace_id = ?) "
                f"{alias} ")

    isolated = TABLE_RE.sub(_sub, sql)
    return isolated, params


async def run(workspace_id: str, sql: str, max_rows: int = MAX_ROWS) -> dict:
    """在**当前租户**数据上执行只读查询"""
    from ..core.store import get_store
    text = validate(sql)
    isolated, params = isolate_tenant(text, workspace_id)
    wrapped = f"SELECT * FROM ({isolated}) AS _q LIMIT {int(max_rows)}"
    store = await get_store()
    rows = await store._fetchall(wrapped, tuple(params))
    cols: list[str] = []
    for r in rows:
        for k in r.keys():
            if k not in cols:
                cols.append(k)
    return {
        "columns": cols,
        "rows": [[r.get(c) for c in cols] for r in rows],
        "count": len(rows),
        "truncated": len(rows) >= max_rows,
        "note": ("只读沙箱：仅白名单表，租户过滤由系统注入（每条表引用都包了 "
                 "workspace_id 子查询），结果上限 %d 行" % max_rows),
    }
