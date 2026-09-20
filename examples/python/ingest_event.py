#!/usr/bin/env python3
"""HMAC 入站一条成交事件。

  IF_INGEST_SECRET=dev-secret IF_WORKSPACE=insflow-demo python3 ingest_event.py
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request

BASE = os.environ.get("IF_BASE", "http://127.0.0.1:8400").rstrip("/")
WS = os.environ.get("IF_WORKSPACE", "insflow-demo")
SECRET = os.environ.get("IF_INGEST_SECRET", "")


def main() -> None:
    if not SECRET:
        raise SystemExit("请设置 IF_INGEST_SECRET（与 INSFLOW_INGEST_SECRET 相同）")
    payload = {
        "schema_version": "1.0",
        "event": "cdp.order",
        "event_id": "ex-order-py-1",
        "source": "example",
        "data": {"identity": "buyer@example.com", "amount": 99.0, "channel": "search"},
    }
    raw = json.dumps(payload, ensure_ascii=False).encode()
    sig = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        f"{BASE}/api/v1/ingest?workspace_id={WS}",
        data=raw,
        method="POST",
        headers={"Content-Type": "application/json", "X-IF-Signature": sig},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        print(resp.read().decode())


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"HTTP {exc.code}: {exc.read().decode()[:400]}") from exc
