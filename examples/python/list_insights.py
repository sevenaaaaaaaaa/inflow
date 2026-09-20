#!/usr/bin/env python3
"""列出洞察（仅标准库）。

  IF_BASE=http://127.0.0.1:8400 IF_WORKSPACE=insflow-demo python3 list_insights.py
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

BASE = os.environ.get("IF_BASE", "http://127.0.0.1:8400").rstrip("/")
WS = os.environ.get("IF_WORKSPACE", "insflow-demo")
KEY = os.environ.get("IF_API_KEY", "")


def get(path: str) -> dict:
    req = urllib.request.Request(f"{BASE}{path}")
    if KEY:
        req.add_header("X-API-Key", KEY)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def main() -> None:
    data = get(f"/api/v1/insights?workspace_id={WS}&limit=5")
    print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"HTTP {exc.code}: {exc.read().decode()[:400]}") from exc
