"""OpenAPI 3.1 契约快照（CI 锁定破坏性变更）

快照路径：`contracts/openapi.json`
更新：`python scripts/export_openapi.py --write`
检查：`python scripts/export_openapi.py --check`（CI）
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_PATH = REPO_ROOT / "contracts" / "openapi.json"


def dump_spec() -> str:
    """当前应用的稳定序列化 OpenAPI（sort_keys，便于 diff）。"""
    from ..server.app import app
    spec = app.openapi()
    return json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_snapshot(path: Path | None = None) -> Path:
    dest = path or SNAPSHOT_PATH
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(dump_spec(), encoding="utf-8")
    return dest


def check_snapshot(path: Path | None = None) -> dict:
    """对比当前 spec 与已提交快照。"""
    dest = path or SNAPSHOT_PATH
    current = dump_spec()
    if not dest.exists():
        return {"ok": False, "reason": f"快照不存在: {dest}",
                "hint": "运行 python scripts/export_openapi.py --write"}
    committed = dest.read_text(encoding="utf-8")
    if committed == current:
        return {"ok": True, "path": str(dest)}
    return {
        "ok": False,
        "reason": "OpenAPI 相对 contracts/openapi.json 有变更（破坏性或新增均需显式更新快照）",
        "hint": "确认变更可接受后运行 python scripts/export_openapi.py --write",
        "path": str(dest),
    }
