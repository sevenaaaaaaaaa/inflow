"""控制台主题令牌（工作区 settings_json.theme_tokens → CSS 变量）

只允许主色 / 圆角 / 密度。非法值丢弃，避免把 CSS 注入写进每页。
"""

from __future__ import annotations

import re

from ..core.store import get_store

HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
OKLCH_RE = re.compile(r"^oklch\([0-9.%\s./+-]+\)$")
RADIUS = ("compact", "default", "relaxed")
DENSITY = ("compact", "default", "comfortable")

RADIUS_VARS = {
    "compact": {"--r-lg": "16px", "--r-md": "10px", "--r-sm": "8px"},
    "relaxed": {"--r-lg": "32px", "--r-md": "22px", "--r-sm": "14px"},
}


def _safe_color(value: str) -> str:
    raw = (value or "").strip()
    if HEX_RE.match(raw) or OKLCH_RE.match(raw):
        return raw
    return ""


def normalize_tokens(raw: dict | None) -> dict:
    data = raw if isinstance(raw, dict) else {}
    accent = _safe_color(str(data.get("accent") or data.get("accent_color") or ""))
    radius = str(data.get("radius") or "default")
    density = str(data.get("density") or "default")
    if radius not in RADIUS:
        radius = "default"
    if density not in DENSITY:
        density = "default"
    return {"accent": accent, "radius": radius, "density": density}


def tokens_to_css(tokens: dict | None) -> str:
    """生成可内联的 `:root` 覆盖。空令牌 → 空字符串（沿用 static_app.css）。"""
    t = normalize_tokens(tokens)
    decls: list[str] = []
    if t["accent"]:
        accent = t["accent"]
        decls += [
            f"--accent:{accent}",
            f"--accent-strong:{accent}",
            f"--accent-soft:color-mix(in oklab, {accent} 16%, transparent)",
            f"--grad:linear-gradient(135deg,{accent},{accent})",
        ]
    decls += [f"{k}:{v}" for k, v in RADIUS_VARS.get(t["radius"], {}).items()]
    if not decls:
        return ""
    body = ";".join(decls)
    return f":root{{{body}}}[data-theme='dark']{{{body}}}"


async def load_tokens(workspace_id: str) -> dict:
    if not workspace_id:
        return normalize_tokens({})
    store = await get_store()
    ws = await store.get_workspace(workspace_id)
    settings = (ws.settings_json if ws else {}) or {}
    return normalize_tokens(settings.get("theme_tokens"))


async def save_tokens(workspace_id: str, raw: dict) -> dict:
    store = await get_store()
    ws = await store.get_workspace(workspace_id)
    if not ws:
        raise ValueError("工作区不存在")
    tokens = normalize_tokens(raw)
    settings = dict(ws.settings_json or {})
    settings["theme_tokens"] = tokens
    # 同步白标主色，报告/水印与控制台同一套
    branding = dict(settings.get("branding") or {})
    if tokens["accent"]:
        branding["accent_color"] = tokens["accent"]
        settings["branding"] = branding
    ws.settings_json = settings
    await store.update_workspace(ws)
    return tokens
