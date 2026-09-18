"""SCIM 2.0 用户/组同步（企业采购常见硬要求，零依赖）

实现 RFC 7643/7644 的最小可用子集：
- `GET /scim/v2/Users`（startIndex/count + filter：`userName eq "x"`、`active eq true`）
- `POST /scim/v2/Users`（创建，含 externalId / active / 角色扩展）
- `PATCH /scim/v2/Users/{id}`（replace active / 角色）
- `PUT /scim/v2/Users/{id}`（整体替换）
- `DELETE /scim/v2/Users/{id}`（停用而非删除：保留审计与历史数据）
- `GET /scim/v2/Groups`（由角色映射出的虚拟组，只读）
- `GET /scim/v2/ServiceProviderConfig`（能力声明）

鉴权：`Authorization: Bearer <token>`，token 来自环境 `INSFLOW_SCIM_TOKEN` 或
工作区设置 `scim_token`；未配置或校验失败一律 401（fail-closed）。
注意：SCIM 是全租户管理员通道，token 视同管理员凭据（勿下发到前端）。
"""

import hmac
import re
from datetime import UTC, datetime

from ..core.store import get_store
from .permissions import ROLES

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
ENTERPRISE_SCHEMA = "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
ROLE_KEY = "role"
FILTER_RE = re.compile(r'^\s*(\w+)\s+eq\s+"([^"]*)"\s*$', re.IGNORECASE)


class ScimError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def configured_token(settings: dict | None) -> str:
    import os
    return str((settings or {}).get("scim_token") or
               os.environ.get("INSFLOW_SCIM_TOKEN", ""))


def check_token(provided: str, settings: dict | None) -> None:
    expected = configured_token(settings)
    if not expected:
        raise ScimError(401, "SCIM 未启用（需配置 INSFLOW_SCIM_TOKEN 或工作区 scim_token）")
    if not provided or not hmac.compare_digest(provided, expected):
        raise ScimError(401, "SCIM 凭据无效")


def _user_resource(row: dict, role: str = "") -> dict:
    active = bool(row.get("active", 1))
    return {
        "schemas": [USER_SCHEMA, ENTERPRISE_SCHEMA],
        "id": row.get("id", ""),
        "externalId": row.get("external_id", "") or row.get("id", ""),
        "userName": row.get("email", ""),
        "name": {"formatted": row.get("name", "") or row.get("email", "")},
        "emails": [{"value": row.get("email", ""), "primary": True}],
        "active": active,
        "displayName": row.get("name", "") or row.get("email", ""),
        ROLE_KEY: role or row.get("role", "viewer"),
        "meta": {"resourceType": "User", "created": row.get("created_at", ""),
                 "location": f"/scim/v2/Users/{row.get('id', '')}"},
    }


async def list_users(workspace_id: str, *, filter_: str = "", start: int = 1,
                     count: int = 100) -> dict:
    store = await get_store()
    rows = await store._fetchall(
        "SELECT id, email, name, role, workspace_id, created_at FROM users "
        "WHERE workspace_id = ? ORDER BY created_at", (workspace_id,))
    rows = [dict(r) for r in rows]
    if filter_:
        m = FILTER_RE.match(filter_)
        if not m:
            raise ScimError(400, f"不支持的 filter：{filter_}（支持 userName eq / active eq）")
        field, value = m.group(1).lower(), m.group(2)
        if field in ("username", "email"):
            rows = [r for r in rows if str(r.get("email", "")).lower() == value.lower()]
        elif field == "active":
            want = value.lower() in ("true", "1")
            rows = [r for r in rows if bool(r.get("active", 1)) is want]
        else:
            raise ScimError(400, f"不支持的过滤字段：{field}")
    total = len(rows)
    start = max(1, int(start))
    page = rows[start - 1: start - 1 + max(1, min(int(count), 200))]
    return {"schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
            "totalResults": total, "startIndex": start,
            "itemsPerPage": len(page),
            "Resources": [_user_resource(r) for r in page]}


async def get_user(workspace_id: str, user_id: str) -> dict:
    store = await get_store()
    row = await store._fetchone(
        "SELECT id, email, name, role, active, external_id, created_at FROM users "
        "WHERE workspace_id = ? AND id = ?", (workspace_id, user_id))
    if not row:
        raise ScimError(404, "用户不存在")
    return _user_resource(dict(row))


def _role_from_payload(payload: dict) -> str:
    role = str(payload.get(ROLE_KEY) or "").strip().lower()
    if not role:
        ext = payload.get(ENTERPRISE_SCHEMA) or {}
        role = str(ext.get(ROLE_KEY) or "").strip().lower()
    if role and role not in ROLES:
        raise ScimError(400, f"未知角色：{role}（可用 {list(ROLES)}）")
    return role


async def create_user(workspace_id: str, payload: dict) -> dict:
    store = await get_store()
    email = str(payload.get("userName") or "").strip().lower()
    if not email:
        raise ScimError(400, "userName（邮箱）必填")
    existing = await store._fetchone(
        "SELECT id, email, name, role, created_at FROM users WHERE email = ?", (email,))
    if existing:                      # 幂等：已存在则返回（IdP 重试常见）
        existing = await get_user(workspace_id, existing["id"])
        existing["x-created"] = False
        return existing
    name = (payload.get("name") or {}).get("formatted") or \
        (payload.get("displayName") or email.split("@")[0])
    role = _role_from_payload(payload) or "viewer"
    # 注意：SCIM 是「加入既有工作区」，不是自助注册——不能走 register()
    # （后者会新建租户并发起试用，语义与副作用都不对）。
    import secrets

    from ..core.accounts import hash_password
    from ..core.store import generate_id
    uid = generate_id()
    now = datetime.now(UTC).isoformat()
    await store._execute(
        """INSERT INTO users (id, email, password_hash, name, role, workspace_id,
           external_id, active, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (uid, email, hash_password(secrets.token_urlsafe(24) + "Aa1!"), name[:128],
         role, workspace_id, str(payload.get("externalId") or "")[:191],
         1 if payload.get("active", True) else 0, now))
    await store._db.commit()
    resource = await get_user(workspace_id, uid)
    resource["x-created"] = True
    return resource


async def patch_user(workspace_id: str, user_id: str, payload: dict) -> dict:
    store = await get_store()
    row = await store._fetchone("SELECT * FROM users WHERE workspace_id = ? AND id = ?",
                                (workspace_id, user_id))
    if not row:
        raise ScimError(404, "用户不存在")
    ops = payload.get("Operations") or payload.get("operations") or []
    updates: dict = {}
    for op in ops:
        path = str(op.get("path") or "").lower()
        value = op.get("value")
        if path == "active" or (path == "" and isinstance(value, dict) and "active" in value):
            active = value if path == "active" else value.get("active")
            updates["active"] = 1 if str(active).lower() in ("true", "1") else 0
        elif path in ("role", "roles") or (isinstance(value, dict) and "role" in value):
            role = (value if isinstance(value, str) else value.get("role", ""))
            role = str(role).strip().lower()
            if role not in ROLES:
                raise ScimError(400, f"未知角色：{role}")
            updates["role"] = role
    if not updates:
        raise ScimError(400, "没有可应用的变更（支持 active / role）")
    sets = ", ".join(f"{k} = ?" for k in updates)
    await store._execute(f"UPDATE users SET {sets} WHERE workspace_id = ? AND id = ?",
                         tuple([*updates.values(), workspace_id, user_id]))
    await store._db.commit()
    return await get_user(workspace_id, user_id)


async def delete_user(workspace_id: str, user_id: str) -> None:
    """停用（软删除）：保留审计与历史归属"""
    store = await get_store()
    cur = await store._execute(
        "UPDATE users SET active = 0 WHERE workspace_id = ? AND id = ?",
        (workspace_id, user_id))
    await store._db.commit()
    if cur.rowcount == 0:
        raise ScimError(404, "用户不存在")


async def list_groups() -> dict:
    """虚拟组：按角色生成（SCIM 组同步多用于角色授予，这里只读暴露）"""
    resources = [{
        "schemas": [GROUP_SCHEMA], "id": role, "displayName": role,
        "members": [],
        "meta": {"resourceType": "Group",
                 "location": f"/scim/v2/Groups/{role}"},
    } for role in ROLES]
    return {"schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
            "totalResults": len(resources), "startIndex": 1,
            "itemsPerPage": len(resources), "Resources": resources}


def service_provider_config() -> dict:
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        "filter": {"supported": True, "maxResults": 200},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [{
            "type": "oauthbearertoken", "name": "Bearer", "primary": True,
        }],
        "meta": {"resourceType": "ServiceProviderConfig",
                 "location": "/scim/v2/ServiceProviderConfig",
                 "created": datetime.now(UTC).isoformat()},
    }
