"""SSO（通用 OIDC，零依赖）：discovery + 授权码流程 + userinfo

为什么走 userinfo 而不是本地验签 id_token：
- 零依赖约束下验 RS256 需要手写 PKCS#1 v1.5 校验（易错且难测）；
- 取 access_token 后直接调 userinfo（TLS 通道 + 服务端校验）更简单、也更安全。

配置（环境变量）：
  INSFLOW_OIDC_ISSUER          如 https://accounts.example.com
  INSFLOW_OIDC_CLIENT_ID
  INSFLOW_OIDC_CLIENT_SECRET
  INSFLOW_OIDC_SCOPES          默认 "openid email profile"
  INSFLOW_OIDC_DEFAULT_ROLE    新用户默认角色，默认 analyst

安全：
- state 随机 + Cookie 校验（CSRF 防护），5 分钟过期
- fail-closed：未配置 issuer/client_id 时登录入口直接拒绝
- 邮箱缺失或未验证（email_verified=false）时拒绝登录
"""

import base64
import hashlib
import json
import os
import secrets
import time
from urllib.parse import urlencode

DISCOVERY_TTL = 3600
_discovery_cache: dict[str, tuple[float, dict]] = {}


class SsoError(Exception):
    pass


def config() -> dict:
    return {
        "issuer": os.environ.get("INSFLOW_OIDC_ISSUER", "").rstrip("/"),
        "client_id": os.environ.get("INSFLOW_OIDC_CLIENT_ID", ""),
        "client_secret": os.environ.get("INSFLOW_OIDC_CLIENT_SECRET", ""),
        "scopes": os.environ.get("INSFLOW_OIDC_SCOPES", "openid email profile"),
        "default_role": os.environ.get("INSFLOW_OIDC_DEFAULT_ROLE", "analyst"),
    }


def enabled() -> bool:
    cfg = config()
    return bool(cfg["issuer"] and cfg["client_id"])


def new_state() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(24)).decode().rstrip("=")


async def _http_json(method: str, url: str, **kw) -> dict:
    """统一出口（测试可 monkeypatch 本函数，避免真实网络）"""
    import httpx
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.request(method, url, **kw)
        resp.raise_for_status()
        return resp.json()


async def discover(issuer: str = "") -> dict:
    """OIDC discovery（带 1 小时缓存）"""
    cfg = config()
    iss = (issuer or cfg["issuer"]).rstrip("/")
    if not iss:
        raise SsoError("未配置 OIDC issuer")
    cached = _discovery_cache.get(iss)
    if cached and time.time() - cached[0] < DISCOVERY_TTL:
        return cached[1]
    doc = await _http_json("GET", f"{iss}/.well-known/openid-configuration")
    for key in ("authorization_endpoint", "token_endpoint"):
        if not doc.get(key):
            raise SsoError(f"discovery 缺少 {key}")
    _discovery_cache[iss] = (time.time(), doc)
    return doc


async def authorize_url(redirect_uri: str, state: str, nonce: str = "") -> str:
    """构造授权跳转 URL"""
    cfg = config()
    if not enabled():
        raise SsoError("未配置 SSO（INSFLOW_OIDC_ISSUER / CLIENT_ID）")
    doc = await discover()
    params = {
        "response_type": "code",
        "client_id": cfg["client_id"],
        "redirect_uri": redirect_uri,
        "scope": cfg["scopes"],
        "state": state,
    }
    if nonce:
        params["nonce"] = nonce
    return f"{doc['authorization_endpoint']}?{urlencode(params)}"


async def exchange_code(code: str, redirect_uri: str) -> dict:
    """授权码 → tokens（client_secret_basic 或 post，按 discovery 能力）"""
    cfg = config()
    doc = await discover()
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": cfg["client_id"],
    }
    auth = None
    if cfg["client_secret"]:
        methods = doc.get("token_endpoint_auth_methods_supported") or []
        if "client_secret_basic" in methods:
            auth = (cfg["client_id"], cfg["client_secret"])
        else:
            data["client_secret"] = cfg["client_secret"]
    tokens = await _http_json("POST", doc["token_endpoint"], data=data, auth=auth)
    if not tokens.get("access_token"):
        raise SsoError("token 端点未返回 access_token")
    return tokens


async def fetch_userinfo(access_token: str) -> dict:
    """userinfo → {email, name, subject, email_verified, raw}"""
    doc = await discover()
    url = doc.get("userinfo_endpoint")
    if not url:
        raise SsoError("discovery 缺少 userinfo_endpoint")
    info = await _http_json("GET", url, headers={"Authorization": f"Bearer {access_token}"})
    email = str(info.get("email") or "").strip().lower()
    if not email:
        raise SsoError("userinfo 未返回 email（无法绑定账号）")
    if info.get("email_verified") is False:
        raise SsoError("邮箱未验证，拒绝登录")
    return {
        "email": email,
        "name": str(info.get("name") or info.get("preferred_username") or email),
        "subject": str(info.get("sub") or ""),
        "role": config()["default_role"],
        "raw": info,
    }


async def upsert_user(workspace_id: str, userinfo: dict) -> dict:
    """按邮箱找用户；没有则按默认角色开通（自建工作区）"""
    from ..core.accounts import AccountManager
    from ..core.store import get_store
    store = await get_store()
    row = await store._fetchone(
        "SELECT id, email, name, role, workspace_id FROM users WHERE email = ?",
        (userinfo["email"],))
    if row:
        return {"user_id": row["id"], "email": row["email"],
                "workspace_id": row["workspace_id"], "role": row["role"],
                "created": False}
    mgr = AccountManager(workspace_id)
    created = await mgr.register(userinfo["email"],
                                 secrets.token_urlsafe(24) + "Aa1!",
                                 userinfo["name"], "SSO 用户")
    # 覆盖角色为配置的默认角色（register 默认 owner）
    await store._execute("UPDATE users SET role = ? WHERE id = ?",
                         (userinfo["role"], created["user_id"]))
    await store._db.commit()
    return {"user_id": created["user_id"], "email": userinfo["email"],
            "workspace_id": created["workspace_id"], "role": userinfo["role"],
            "created": True}


async def start_session(user_id: str) -> str:
    """为已存在用户建立会话（复用 AccountManager 的会话表）"""
    from datetime import UTC, datetime

    from ..core.accounts import AccountManager
    return await AccountManager()._create_session(user_id, datetime.now(UTC))


def state_cookie_name() -> str:
    return "if_oidc_state"


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]
