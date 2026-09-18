"""Insight Flow 多租户自助账号（R4-1）

面向超级个体的无人工注册/登录：
- 注册：邮箱 + 密码 → 建账号 + 建租户（workspace）+ 绑定 Free 套餐
- 登录：密码校验 → 会话令牌（Cookie）
- 会话：令牌落库 + 过期；SaaS 模式下控制台全量要求登录（fail-closed）

密码哈希：hashlib.scrypt（stdlib，无额外依赖）
租户隔离：每个账号默认独占一个 workspace（后续可扩展多 workspace 成员）
"""

import hashlib
import hmac
import os
import secrets
from datetime import UTC, datetime, timedelta

from .files import EventBus
from .store import get_store

SESSION_TTL_DAYS = 30
COOKIE_NAME = "if_session"

# SaaS 模式开关：开启后控制台需登录
def SAAS_MODE() -> bool:
    """SaaS 模式开关（开启后控制台需登录）"""
    return os.environ.get("INSFLOW_SAAS", "") == "1"


class AccountError(Exception):
    """账号操作错误（对外可作为 4xx 提示）"""
    pass


def hash_password(password: str, salt: str | None = None) -> str:
    """scrypt 密码哈希（格式 scrypt$salt$hash）"""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=salt.encode(),
                            n=2 ** 14, r=8, p=1, dklen=32).hex()
    return f"scrypt${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    """常量时间校验"""
    try:
        scheme, salt, _ = stored.split("$", 2)
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    candidate = hash_password(password, salt)
    return hmac.compare_digest(candidate, stored)


class AccountManager:
    """账号与会话管理"""

    def __init__(self, workspace_id: str = "default"):
        self.workspace_id = workspace_id
        self.bus = EventBus(workspace_id)

    # ========== 注册 ==========

    async def register(self, email: str, password: str, name: str = "",
                       workspace_name: str = "") -> dict:
        """注册：建账号 + 建租户 + 绑 Free 套餐 → 返回会话令牌

        Raises:
            AccountError: 邮箱格式/密码强度/重复注册
        """
        email = (email or "").strip().lower()
        if "@" not in email or "." not in email.split("@")[-1]:
            raise AccountError("邮箱格式不正确")
        if len(password or "") < 8:
            raise AccountError("密码至少 8 位")

        store = await get_store()
        existing = await store._fetchone("SELECT id FROM users WHERE email = ?", (email,))
        if existing:
            raise AccountError("该邮箱已注册")

        # 建租户（租户隔离：每账号一个 workspace）
        from .entities import Workspace
        tenant = await store.create_workspace(
            Workspace(name=workspace_name or f"{name or email.split('@')[0]} 的工作区")
        )
        # 绑定 Free 套餐
        tenant.settings_json = {"plan": "free"}
        await store.update_workspace(tenant)

        uid = secrets.token_hex(6)
        now = datetime.now(UTC)
        await store._execute(
            """INSERT INTO users (id, email, password_hash, name, role, workspace_id, created_at)
               VALUES (?, ?, ?, ?, 'owner', ?, ?)""",
            (uid, email, hash_password(password), name or email.split("@")[0],
             tenant.id, now.isoformat()),
        )
        await store._db.commit()

        token = await self._create_session(uid, now)

        # 自助试用（G-4）：注册即得 14 天 Growth 体验
        trial = {}
        try:
            from ..engine.billing import TRIAL_DAYS, TRIAL_PLAN, BillingManager
            trial = await BillingManager(tenant.id).start_trial()
            trial["days"] = TRIAL_DAYS
            trial["plan"] = TRIAL_PLAN
        except Exception:
            trial = {"ok": False}

        EventBus(tenant.id).emit("account.registered", {
            "user_id": uid, "email": email, "workspace_id": tenant.id,
            "trial": trial.get("plan", ""),
        })
        return {"user_id": uid, "email": email, "workspace_id": tenant.id,
                "token": token, "trial": trial}

    # ========== 登录 ==========

    async def login(self, email: str, password: str) -> dict:
        """登录 → 会话令牌"""
        email = (email or "").strip().lower()
        store = await get_store()
        row = await store._fetchone("SELECT * FROM users WHERE email = ?", (email,))
        if not row or not verify_password(password or "", row["password_hash"]):
            raise AccountError("邮箱或密码不正确")
        token = await self._create_session(row["id"], datetime.now(UTC))
        EventBus(row["workspace_id"] or self.workspace_id).emit("account.login", {
            "user_id": row["id"], "email": email,
        })
        return {"user_id": row["id"], "email": email,
                "workspace_id": row["workspace_id"], "token": token}

    # ========== 会话 ==========

    async def _create_session(self, user_id: str, now: datetime) -> str:
        token = secrets.token_urlsafe(32)
        store = await get_store()
        await store._execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, user_id, now.isoformat(),
             (now + timedelta(days=SESSION_TTL_DAYS)).isoformat()),
        )
        await store._db.commit()
        return token

    async def verify_session(self, token: str | None) -> dict | None:
        """校验会话令牌 → 用户信息（过期/无效返回 None）"""
        if not token:
            return None
        store = await get_store()
        row = await store._fetchone(
            "SELECT * FROM sessions WHERE token = ?", (token,))
        if not row:
            return None
        try:
            if datetime.fromisoformat(row["expires_at"]) < datetime.now(UTC):
                await self.logout(token)
                return None
        except (ValueError, TypeError):
            return None
        user = await store._fetchone("SELECT * FROM users WHERE id = ?", (row["user_id"],))
        if not user:
            return None
        return {"user_id": user["id"], "email": user["email"], "name": user["name"],
                "role": user["role"], "workspace_id": user["workspace_id"]}

    async def logout(self, token: str | None) -> None:
        if not token:
            return
        store = await get_store()
        await store._execute("DELETE FROM sessions WHERE token = ?", (token,))
        await store._db.commit()

    # ========== 清理（调度器可挂）==========

    async def purge_expired_sessions(self) -> int:
        store = await get_store()
        cur = await store._execute(
            "DELETE FROM sessions WHERE expires_at < ?",
            (datetime.now(UTC).isoformat(),))
        await store._db.commit()
        return cur.rowcount
