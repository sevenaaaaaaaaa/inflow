"""工作区自定义看板（选面板 → 存 settings_json.custom_boards → 渲染/嵌入）"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from ..core.store import get_store

BOARD_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,47}$")
MAX_PANELS = 12
MAX_BOARDS = 20

# slot 对应 build_board_panels 的 csv 后缀（cockpit-slot）
PANEL_CATALOG: list[dict] = [
    {"id": "overview.all", "cockpit": "overview", "slot": "all",
     "title": "情报总览（全部面板）"},
    {"id": "traffic.geo", "cockpit": "traffic", "slot": "geo",
     "title": "流量 · 地域"},
    {"id": "traffic.channels", "cockpit": "traffic", "slot": "channels",
     "title": "流量 · 渠道"},
    {"id": "sentiment.risk", "cockpit": "sentiment", "slot": "risk",
     "title": "舆情 · 负向占比"},
    {"id": "competitor.gaps", "cockpit": "competitor", "slot": "gaps",
     "title": "竞品 · 关键词缺口"},
    {"id": "competitor.all", "cockpit": "competitor", "slot": "all",
     "title": "竞品（全部面板）"},
    {"id": "journey.all", "cockpit": "journey", "slot": "all",
     "title": "旅程（全部面板）"},
    {"id": "action-loop.all", "cockpit": "action-loop", "slot": "all",
     "title": "行动验证（全部面板）"},
    {"id": "ops.all", "cockpit": "ops", "slot": "all",
     "title": "运维（全部面板）"},
    {"id": "billing.all", "cockpit": "billing", "slot": "all",
     "title": "商业化（全部面板）"},
]

CATALOG_BY_ID = {p["id"]: p for p in PANEL_CATALOG}


def _slug(name: str) -> str:
    raw = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    if BOARD_ID_RE.match(raw):
        return raw
    return f"board-{abs(hash(name)) % 10**8}"


def list_from_settings(settings: dict | None) -> list[dict]:
    boards = (settings or {}).get("custom_boards") or []
    return [b for b in boards if isinstance(b, dict) and b.get("id")]


def get_board(settings: dict | None, board_id: str) -> dict | None:
    for b in list_from_settings(settings):
        if b.get("id") == board_id:
            return b
    return None


async def _ws(workspace_id: str):
    store = await get_store()
    ws = await store.get_workspace(workspace_id)
    if not ws:
        raise ValueError("工作区不存在")
    return store, ws, dict(ws.settings_json or {})


async def list_boards(workspace_id: str) -> list[dict]:
    _, _, settings = await _ws(workspace_id)
    return list_from_settings(settings)


def validate_panel_ids(panel_ids: list) -> list[str]:
    out: list[str] = []
    for pid in panel_ids or []:
        key = str(pid)
        if key in CATALOG_BY_ID and key not in out:
            out.append(key)
        if len(out) >= MAX_PANELS:
            break
    return out


async def save_board(workspace_id: str, *, name: str, panel_ids: list,
                     board_id: str = "") -> dict:
    store, ws, settings = await _ws(workspace_id)
    boards = list_from_settings(settings)
    bid = (board_id or _slug(name)).lower()
    if not BOARD_ID_RE.match(bid):
        raise ValueError("看板 id 须为小写字母开头、仅含 a-z0-9-")
    title = (name or bid).strip()[:40] or bid
    panels = validate_panel_ids(panel_ids)
    if not panels:
        raise ValueError("至少选择一块面板")
    now = datetime.now(UTC).isoformat()
    existing = next((b for b in boards if b["id"] == bid), None)
    body = {"id": bid, "name": title, "panel_ids": panels,
            "updated_at": now,
            "created_at": (existing or {}).get("created_at") or now}
    if existing:
        boards = [body if b["id"] == bid else b for b in boards]
    else:
        if len(boards) >= MAX_BOARDS:
            raise ValueError(f"最多 {MAX_BOARDS} 个自定义看板")
        boards.append(body)
    settings["custom_boards"] = boards
    ws.settings_json = settings
    await store.update_workspace(ws)
    return body


async def delete_board(workspace_id: str, board_id: str) -> bool:
    store, ws, settings = await _ws(workspace_id)
    boards = list_from_settings(settings)
    nxt = [b for b in boards if b.get("id") != board_id]
    if len(nxt) == len(boards):
        return False
    settings["custom_boards"] = nxt
    ws.settings_json = settings
    await store.update_workspace(ws)
    return True


def _match_panels(built: list[dict], slot: str) -> list[dict]:
    if slot == "all":
        return built
    return [p for p in built if str(p.get("csv") or "").endswith(f"-{slot}")
            or slot in str(p.get("title") or "")]


async def render_panels(workspace_id: str, board: dict, *, days: float = 14) -> list[dict]:
    """按看板定义拉驾驶舱数据 → datapanel 结构。"""
    from ..web.cockpit import COCKPITS
    from ..web.routes import build_board_panels

    wanted = [CATALOG_BY_ID[i] for i in board.get("panel_ids") or []
              if i in CATALOG_BY_ID]
    cache: dict[str, list[dict]] = {}
    out: list[dict] = []
    for spec in wanted:
        name = spec["cockpit"]
        if name not in cache:
            fn = COCKPITS.get(name)
            data = {}
            if fn:
                try:
                    data = await fn(workspace_id, days)
                except TypeError:
                    data = await fn(workspace_id)
            cache[name] = build_board_panels(name, data or {})
        out.extend(_match_panels(cache[name], spec["slot"]))
    # 保序去重（同 csv）
    seen: set[str] = set()
    uniq = []
    for p in out:
        key = str(p.get("csv") or p.get("title"))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    return uniq[:MAX_PANELS]
