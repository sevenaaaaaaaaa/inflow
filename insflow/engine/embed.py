"""Insight Flow 嵌入交付（D1：embed token + 只读面板）

场景：顾问/代理把客户的某个指标或某个驾驶舱嵌入到自己的交付页面/网站。
安全：HMAC-SHA256 签名令牌（含工作区、面板、到期时间），fail-closed；
      只读（不含控制台导航、不暴露写操作）；支持白标（公司名/主色/logo）。
"""

import base64
import hashlib
import hmac
import json
import os
import time



class EmbedError(Exception):
    pass


def _secret() -> str:
    secret = os.environ.get("INSFLOW_EMBED_SECRET", "")
    if not secret:
        # 回落：主密钥派生（保证私有化零额外配置，仍 fail-closed）
        master = os.environ.get("INSFLOW_MASTER_KEY", "")
        if not master:
            raise EmbedError("需设置 INSFLOW_EMBED_SECRET 或 INSFLOW_MASTER_KEY（fail-closed）")
        secret = hashlib.sha256(f"embed:{master}".encode()).hexdigest()
    return secret


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def mint(workspace_id: str, panel: str, hours: int = 24 * 30, *,
         branding: dict | None = None) -> str:
    """签发嵌入令牌（panel 形如 cockpit:traffic 或 metric:gsc_clicks）"""
    payload = {
        "w": workspace_id,
        "p": panel,
        "e": int(time.time()) + hours * 3600,
        "b": branding or {},
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    sig = hmac.new(_secret().encode(), raw, hashlib.sha256).digest()
    return f"{_b64(raw)}.{_b64(sig)}"


def verify(token: str) -> dict:
    """校验令牌 → payload（无效/过期抛 EmbedError）"""
    if not token or "." not in token:
        raise EmbedError("令牌格式不正确")
    body_b64, sig_b64 = token.split(".", 1)
    try:
        raw = _unb64(body_b64)
        sig = _unb64(sig_b64)
    except Exception as e:
        raise EmbedError("令牌解码失败") from e
    expected = hmac.new(_secret().encode(), raw, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise EmbedError("签名校验失败")
    payload = json.loads(raw)
    if int(payload.get("e", 0)) < time.time():
        raise EmbedError("令牌已过期")
    return payload
