"""Insight Flow RBAC（PL-6）+ API Key 多 Key 认证 + 审计

角色矩阵（PL-6，对齐 OpenFlow ApiPolicy）：
| 动作                    | Owner | Admin | Analyst | Viewer |
|------------------------|-------|-------|---------|--------|
| 读（洞察/报告）           | ✓     | ✓     | ✓       | ✓      |
| 触发诊断/问答             | ✓     | ✓     | ✓       | ✗      |
| 派发动作/触发剧本         | ✓     | ✓     | ✓       | ✗      |
| 管理模型/插件/Skill       | ✓     | ✓     | ✗       | ✗      |
| 管理工作区/密钥/RBAC      | ✓     | ✗     | ✗       | ✗      |

API Key 体系（多 Key，对齐 OpenFlow ApiKeyAuth）：
- key 随机生成，key_id 标识
- 按 Key 授权 scope（read / write / admin）
- 到期时间 + 启停
"""

import hashlib
import hmac
import secrets as pysecrets
from datetime import UTC, datetime, timedelta
from enum import Enum

from .files import EventBus


class Role(str, Enum):
    OWNER = "owner"
    ADMIN = "admin"
    ANALYST = "analyst"
    VIEWER = "viewer"


ROLE_LEVEL = {
    Role.VIEWER: 0,
    Role.ANALYST: 1,
    Role.ADMIN: 2,
    Role.OWNER: 3,
}

# 权限动作 → 允许的最低角色（PL-6 矩阵）
ACTION_MIN_ROLE = {
    "read": Role.VIEWER,
    "diagnose": Role.ANALYST,
    "dispatch_action": Role.ANALYST,
    "manage_models": Role.ADMIN,
    "manage_workspace": Role.OWNER,
}


class AuthError(Exception):
    pass


def role_at_least(role: str | Role, minimum: str | Role) -> bool:
    """角色权限判断：role >= minimum"""
    try:
        r = Role(role.value if hasattr(role, "value") else str(role))
        m = Role(minimum.value if hasattr(minimum, "value") else str(minimum))
    except ValueError:
        return False
    return ROLE_LEVEL[r] >= ROLE_LEVEL[m]


def can(role: str | Role, action: str) -> bool:
    """RBAC 核心：can(role, action)"""
    minimum = ACTION_MIN_ROLE.get(action)
    if not minimum:
        return False
    return role_at_least(role, minimum)


# ========== API Key 多 Key 体系 ==========

SCOPE_LEVEL = {"read": 0, "write": 1, "admin": 2}

ACTION_REQUIRED_SCOPE = {
    "read": "read",
    "diagnose": "write",
    "dispatch_action": "write",
    "manage_models": "write",
    "manage_workspace": "admin",
}


def hash_secret(secret: str) -> str:
    """API secret 存储（SHA-256）"""
    return hashlib.sha256(secret.encode()).hexdigest()


def mask_key(key: str) -> str:
    if len(key) <= 8:
        return "****"
    return key[:4] + "****" + key[-4:]


def utcnow() -> datetime:
    return datetime.now(UTC)


def verify_hmac_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """HMAC-SHA256 常量时间比较"""
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


class APIKey:
    """API Key（多 Key 体系）"""

    def __init__(self, key_id: str, key: str, name: str = "",
                 scopes: list[str] | None = None,
                 expires_at: datetime | None = None,
                 enabled: bool = True,
                 created_at: datetime | None = None):
        self.key_id = key_id
        self.key = key
        self.name = name
        self.scopes = scopes or ["read"]
        self.expires_at = expires_at
        self.enabled = enabled
        self.created_at = created_at or utcnow()

    def valid(self, now: datetime | None = None) -> bool:
        now = now or utcnow()
        if not self.enabled:
            return False
        if self.expires_at and now > self.expires_at:
            return False
        return True

    def authorize(self, action: str) -> bool:
        """按 scope 授权"""
        required = ACTION_REQUIRED_SCOPE.get(action, "admin")
        max_scope = max((SCOPE_LEVEL.get(s, 99) for s in self.scopes), default=99)
        return max_scope >= SCOPE_LEVEL.get(required, 99)

    def to_dict(self, masked: bool = True) -> dict:
        return {
            "key_id": self.key_id,
            "key": mask_key(self.key) if masked else self.key,
            "name": self.name,
            "scopes": self.scopes,
            "enabled": self.enabled,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


class AuthManager:
    """认证管理器（Key 创建/认证/授权/吊销，全量审计）"""

    def __init__(self, workspace_id: str = "default"):
        self.workspace_id = workspace_id
        self._keys: dict[str, APIKey] = {}
        self.bus = EventBus(workspace_id)

    def create_key(self, name: str, scopes: list[str],
                   ttl_days: int | None = 365) -> APIKey:
        """创建 Key（明文 key 只在创建时返回一次）"""
        key = "ifk_" + pysecrets.token_hex(20)
        exp = (utcnow() + timedelta(days=ttl_days)) if ttl_days else None
        api_key = APIKey(
            key_id=f"k_{pysecrets.token_hex(4)}",
            key=key, name=name, scopes=scopes, expires_at=exp,
        )
        self._keys[api_key.key_id] = api_key
        self.bus.emit("auth.key_created", {
            "key_id": api_key.key_id, "name": name, "scopes": scopes,
        })
        return api_key

    def authenticate(self, key: str) -> APIKey | None:
        """按 key 查找有效 Key（常量时间比较防时序）"""
        for k in self._keys.values():
            if hmac.compare_digest(k.key, key) and k.valid():
                return k
        return None

    def authorize(self, key: str, action: str) -> tuple[bool, APIKey | None]:
        """认证 + 授权一体（失败写审计）"""
        api_key = self.authenticate(key)
        if not api_key:
            self.bus.emit("auth.failed", {"action": action})
            return False, None
        if not api_key.authorize(action):
            self.bus.emit("auth.denied", {"action": action, "key_id": api_key.key_id})
            return False, api_key
        return True, api_key

    def revoke(self, key_id: str) -> bool:
        k = self._keys.get(key_id)
        if k:
            k.enabled = False
            self.bus.emit("auth.key_revoked", {"key_id": key_id})
            return True
        return False

    def list_keys(self) -> list[dict]:
        return [k.to_dict() for k in self._keys.values()]
