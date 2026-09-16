"""Insight Flow 第一方接入向导（TD-1）

GSC / GA4 OAuth2 授权码流程 + CrUX API Key 直配：
1. 生成授权 URL（state 防伪造，回调解码 code → access_token）
2. 凭据入保险库（AES-GCM，只显尾四位）
3. 健康检查（实测能否拉数，DM-2 自报 vs 实测的数据基础）
4. source 实例注册（接入向导完成即产生可用数据源）

环境变量（Google Cloud 应用凭据）：
- GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET
- INSFLOW_BASE_URL（OAuth redirect base，如 https://if.example.com）
"""

import base64
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from ..core.files import EventBus
from ..core.security import get_vault

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# GSC 与 GA4 共用一个 Google OAuth client，scope 不同
SCOPES = {
    "gsc": ["https://www.googleapis.com/auth/webmasters.readonly"],
    "ga4": ["https://www.googleapis.com/auth/analytics.readonly"],
}


class OAuthState:
    """OAuth state 会话（防 CSRF，5 分钟过期）"""

    _sessions: dict[str, dict] = {}

    @classmethod
    def create(cls, workspace_id: str, provider: str) -> str:
        state = secrets.token_urlsafe(24)
        cls._sessions[state] = {
            "workspace_id": workspace_id,
            "provider": provider,
            "created_at": time.time(),
        }
        return state

    @classmethod
    def consume(cls, state: str) -> dict | None:
        """取用即失效（一次性）+ 5 分钟过期"""
        session = cls._sessions.pop(state, None)
        if not session:
            return None
        if time.time() - session["created_at"] > 300:
            return None
        return session


class OnboardingService:
    """第一方接入向导（TD-1）"""

    def __init__(self, workspace_id: str):
        self.workspace_id = workspace_id
        self.bus = EventBus(workspace_id)

    # ========== OAuth 授权 URL ==========

    def auth_url(self, provider: str, client_id: str, redirect_uri: str) -> str:
        """生成 Google OAuth 授权 URL（GSC/GA4）"""
        if provider not in SCOPES:
            raise ValueError(f"不支持的 OAuth 提供方: {provider}")
        state = OAuthState.create(self.workspace_id, provider)
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES[provider]),
            "access_type": "offline",     # 需要 refresh_token
            "prompt": "consent",
            "state": state,
        }
        return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"

    # ========== OAuth 回调 ==========

    async def exchange_code(self, provider: str, code: str, state: str,
                            client_id: str, client_secret: str,
                            redirect_uri: str) -> dict:
        """授权码 → access_token/refresh_token → 保险库"""
        session = OAuthState.consume(state)
        if not session:
            return {"ok": False, "error": "state 无效或已过期（防伪造/防重放）"}
        if session["provider"] != provider or session["workspace_id"] != self.workspace_id:
            return {"ok": False, "error": "state 与请求不匹配"}

        async with httpx.AsyncClient() as client:
            resp = await client.post(GOOGLE_TOKEN_URL, data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30.0)
            resp.raise_for_status()
            token_data = resp.json()

        if "access_token" not in token_data:
            return {"ok": False, "error": f"token 交换失败: {token_data.get('error_description', token_data)}"}

        # 凭据入保险库（只存，不回显；带过期时间供自动轮换）
        vault = get_vault()
        vault_key = f"{provider}_oauth_tokens"
        from datetime import datetime, timedelta, timezone
        expires_in = int(token_data.get("expires_in", 3600))
        vault.set(vault_key, encode_json({
            "access_token": token_data["access_token"],
            "refresh_token": token_data.get("refresh_token", ""),
            "expires_in": expires_in,
            "expires_at": (datetime.now(timezone.utc)
                           + timedelta(seconds=expires_in)).isoformat(),
            "workspace_id": self.workspace_id,
        }))
        vault.save()
        self.bus.emit("source.credentials_saved", {
            "provider": provider, "via": "oauth", "vault_key": vault_key,
        })
        return {"ok": True, "provider": provider, "has_refresh": bool(token_data.get("refresh_token"))}

    # ========== CrUX / API Key 降级路径 ==========

    async def save_api_credential(self, provider: str, credential: str, site: str = "") -> dict:
        """API Key 类凭据（CrUX 等）或手动粘贴的 access_token → 保险库"""
        if provider not in ("crux", "gsc", "ga4", "firecrawl", "serper", "brave",
                            "dataforseo", "reddit", "zhihu"):
            return {"ok": False, "error": f"不支持的提供方: {provider}"}

        vault = get_vault()
        vault.set(f"{provider}_api_key", credential)
        if site:
            vault.set(f"{provider}_site", site)
        vault.save()
        self.bus.emit("source.credentials_saved", {"provider": provider, "via": "api_key"})
        return {"ok": True, "provider": provider}

    async def check_health(self, provider: str) -> dict:
        """接入健康检查（DM-2 实测能否拉数）"""
        vault = get_vault()
        try:
            if provider in ("gsc", "ga4"):
                raw = vault.get(f"{provider}_oauth_tokens")
                if not raw:
                    return {"ok": False, "provider": provider, "detail": "未授权"}
                tokens = decode_json(raw)
                token = tokens.get("access_token", "")
                if not token:
                    return {"ok": False, "provider": provider, "detail": "token 缺失"}
                ok = await self._probe_google(provider, token)
                return {"ok": ok, "provider": provider,
                        "detail": "凭据有效" if ok else "凭据无效或已过期"}
            elif provider == "crux":
                api_key = vault.get("crux_api_key") or ""
                if not api_key:
                    return {"ok": False, "provider": provider, "detail": "未配置 API Key"}
                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        "https://chromeuxreport.googleapis.com/v1/records:queryRecord"
                        f"?key={api_key}",
                        json={"origin": "https://example.com"}, timeout=10.0)
                # 200（有数据）或 404（origin 无数据但 key 有效）
                ok = resp.status_code in (200, 404)
                return {"ok": ok, "provider": provider,
                        "detail": "Key 有效" if ok else f"Key 无效（HTTP {resp.status_code}）"}
            return {"ok": False, "provider": provider, "detail": "不支持的健康检查"}
        except Exception as e:
            return {"ok": False, "provider": provider, "detail": f"{type(e).__name__}: {e}"}

    async def _probe_google(self, provider: str, token: str) -> bool:
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient() as client:
            if provider == "gsc":
                resp = await client.get(
                    "https://searchconsole.googleapis.com/webmasters/v3/sites",
                    headers=headers, timeout=10.0)
                return resp.status_code == 200
            # ga4：用 metadata 端点探测
            resp = await client.get(
                "https://analyticsdata.googleapis.com/v1beta/properties/0:metadata",
                headers=headers, timeout=10.0)
            # 200 或 403（有登录身份但无该 property 权限）都说明 token 有效
            return resp.status_code in (200, 403)


def encode_json(data: dict) -> str:
    import json
    return json.dumps(data, ensure_ascii=False)


def decode_json(raw: str) -> dict:
    import json
    return json.loads(raw)
