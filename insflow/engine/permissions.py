"""角色与行级权限（多角色 + 实体白名单）

角色矩阵（fail-closed：未列出的动作一律拒绝）：
- owner    全部（含计费、成员、密钥）
- admin    除计费外全部
- analyst  读取 + 洞察/动作/订阅/告警/探索（不能改成员/密钥/计费）
- viewer   只读控制台与导出；可被 entity 白名单限制到特定主体

行级权限（RLS-lite）：viewer/analyst 可在工作区设置里配置
`role_entity_allow: {"viewer": ["某品牌"], "analyst": []}`，
空列表 = 不限制；非空时其查询只返回白名单主体的数据。
"""

ROLES = ("owner", "admin", "analyst", "viewer")

MATRIX: dict[str, set[str]] = {
    "owner": {"*"},
    "admin": {"read", "insight.write", "action.write", "subscription.write",
              "alert.write", "explore.sql", "export", "monitor.write",
              "workspace.write"},
    "analyst": {"read", "insight.write", "action.write", "subscription.write",
                "alert.write", "explore.sql", "export"},
    "viewer": {"read", "export"},
}


class PermissionError_(Exception):
    pass


def allowed(role: str, action: str) -> bool:
    caps = MATRIX.get((role or "viewer").lower(), MATRIX["viewer"])
    return "*" in caps or action in caps


def require(role: str, action: str) -> None:
    if not allowed(role, action):
        raise PermissionError_(f"角色 {role or 'viewer'} 无权限执行 {action}")


def entity_allow(settings: dict | None, role: str) -> list[str]:
    """行级权限：该角色可见的主体白名单（空 = 不限制）"""
    limits = dict((settings or {}).get("role_entity_allow") or {})
    values = limits.get((role or "viewer").lower()) or []
    return [str(v) for v in values if v]


def scope_workspace_id(request_workspace: str, settings: dict | None,
                       role: str) -> str:
    """占位：workspace 级隔离已由各查询的 workspace_id 保证；本函数保留扩展点"""
    return request_workspace


def entity_filter_sql(allow: list[str]) -> tuple[str, list]:
    """实体白名单 → SQL 条件（参数化）"""
    if not allow:
        return "", []
    marks = ", ".join(["?"] * len(allow))
    return f" AND entity_id IN ({marks})", list(allow)
