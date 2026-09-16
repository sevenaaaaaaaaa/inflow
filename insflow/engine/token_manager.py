"""OAuth Token 自动轮换（R1-1，修复 v1.0 已知边界）

Google OAuth access_token 有效期约 1 小时。本模块让客户授权 90 天不中断：
1. 保险库 token 记录带 expires_at（交换时写入）
2. 使用时预轮换：距过期 < 10 分钟自动用 refresh_token 换新（grant_type=refresh_token）
3. 401 降级：请求被拒时强制轮换一次并回写保险库（老记录无 expires_at 的兜底路径）
4. refresh 失败 → source.token_refresh_failed 事件（进入告警出站链路）

安全约束：refresh_token 永不回显；轮换后旧 access_token 立即失效；
refresh_token 缺失时不可轮换（Google 首次授权 access_type=offline 才有）。
"""

import json
from datetime import UTC, datetime, timedelta, timezone

import httpx

from ..core.files import EventBus
from ..core.security import get_vault

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

REFRESH_BUFFER_SECONDS = 600  # 提前 10 分钟视为临期


class TokenRefreshError(Exception):
    """token 轮换失败（含保险库无 refresh_token 的情况）"""
    pass


class TokenManager:
    """Google OAuth token 生命周期管理（保险库持久化）"""

    def __init__(self, workspace_id: str = "default",
                 client_id: str | None = None, client_secret: str | None = None):
        import os
        self.workspace_id = workspace_id
        self.client_id = client_id or os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
        self.client_secret = client_secret or os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
        self.bus = EventBus(workspace_id)

    # ========== 读取 ==========

    def load_tokens(self, provider: str) -> dict | None:
        raw = get_vault().get(f"{provider}_oauth_tokens")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None

    def save_tokens(self, provider: str, tokens: dict) -> None:
        """写回保险库（加密落盘）"""
        vault = get_vault()
        vault.set(f"{provider}_oauth_tokens", json.dumps(tokens, ensure_ascii=False))
        vault.save()

    # ========== 过期预警（R2-3：7 天前置预警，避免静默失效事故）==========

    def expiry_status(self, provider: str) -> dict:
        """检查授权剩余有效期

        Returns:
            {"provider", "state": ok|expiring_soon|expired|missing|legacy,
             "remaining_days": float|None}
        """
        tokens = self.load_tokens(provider)
        if not tokens:
            return {"provider": provider, "state": "missing", "remaining_days": None}
        expires_at = tokens.get("expires_at")
        if not expires_at:
            return {"provider": provider, "state": "legacy", "remaining_days": None}
        try:
            exp = datetime.fromisoformat(expires_at)
        except (ValueError, TypeError):
            return {"provider": provider, "state": "legacy", "remaining_days": None}
        remaining = (exp - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            state = "expired"
        elif remaining <= 86400 * 7:
            state = "expiring_soon"
        else:
            state = "ok"
        return {"provider": provider, "state": state,
                "remaining_days": round(remaining / 86400, 1)}

    def audit_all(self) -> list[dict]:
        """每日任务调用：全部 OAuth provider 的过期状态（进事件流供告警出站）"""
        statuses = []
        for provider in ("gsc", "ga4"):
            status = self.expiry_status(provider)
            if status["state"] in ("expiring_soon", "expired"):
                self.bus.emit("auth.token_expiring", status)
            statuses.append(status)
        return statuses



    def ensure_fresh(self, provider: str, buffer_seconds: int = REFRESH_BUFFER_SECONDS) -> str:
        """返回可用的 access_token；临期自动轮换

        Raises:
            TokenRefreshError: 无凭据/无 refresh_token/轮换请求失败
        """
        tokens = self.load_tokens(provider)
        if not tokens:
            raise TokenRefreshError(f"{provider} 未授权（接入向导未完成）")

        access = tokens.get("access_token", "")
        expires_at = tokens.get("expires_at")

        # 无过期时间的旧记录：信任至 401 降级路径触发轮换
        if expires_at:
            try:
                exp = datetime.fromisoformat(expires_at)
                if (exp - datetime.now(UTC)).total_seconds() < buffer_seconds:
                    return self._refresh(provider, tokens)
            except (ValueError, TypeError):
                pass  # expires_at 非法 → 走 401 降级
        if not access:
            return self._refresh(provider, tokens)
        return access

    def force_refresh(self, provider: str) -> str:
        """401 降级路径：无条件轮换（刷新失败则抛错，调用方告警）"""
        tokens = self.load_tokens(provider)
        if not tokens:
            raise TokenRefreshError(f"{provider} 未授权")
        return self._refresh(provider, tokens)

    # ========== 轮换实现 ==========

    def _refresh(self, provider: str, tokens: dict) -> str:
        if not self.client_id or not self.client_secret:
            raise TokenRefreshError("GOOGLE_OAUTH_CLIENT_ID/SECRET 未配置")
        refresh_token = tokens.get("refresh_token", "")
        if not refresh_token:
            self.bus.emit("source.token_refresh_failed", {
                "provider": provider, "reason": "no_refresh_token",
            })
            raise TokenRefreshError(f"{provider} 无 refresh_token（需重新走授权向导）")

        try:
            resp = httpx.post(GOOGLE_TOKEN_URL, data={
                "refresh_token": refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
            }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30.0)
            data = resp.json()  # 先解析（invalid_grant 也在 400 响应体里）
        except (httpx.HTTPError, ValueError) as e:
            self.bus.emit("source.token_refresh_failed", {
                "provider": provider, "error": f"{type(e).__name__}: {e}",
            })
            raise TokenRefreshError(f"Google token 轮换请求失败: {e}") from e

        if "access_token" not in data:
            # refresh_token 无效/被撤销 → 需要重新授权（无静默恢复路径）
            self.bus.emit("source.token_refresh_failed", {
                "provider": provider,
                "error": data.get("error_description", data.get("error", "unknown")),
            })
            raise TokenRefreshError(
                f"{provider} 轮换被拒（{data.get('error')}）：refresh_token 失效，需重新授权"
            )

        now = datetime.now(UTC)
        expires_in = int(data.get("expires_in", 3600))
        new_tokens = {
            **tokens,
            "access_token": data["access_token"],
            "expires_at": (now + timedelta(seconds=expires_in)).isoformat(),
        }
        # Google 在 refresh_token 轮换时可能下发新 refresh_token
        if data.get("refresh_token"):
            new_tokens["refresh_token"] = data["refresh_token"]

        self.save_tokens(provider, new_tokens)
        self.bus.emit("source.token_refreshed", {
            "provider": provider, "expires_at": new_tokens["expires_at"],
        })
        return new_tokens["access_token"]
