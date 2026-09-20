#!/usr/bin/env bash
# HMAC 入站一条 cdp.order（幂等：同一 event_id 重放返回 duplicate）。
#   IF_INGEST_SECRET=dev-secret IF_WORKSPACE=insflow-demo ./ingest.sh
set -euo pipefail
: "${IF_BASE:=http://127.0.0.1:8400}"
: "${IF_WORKSPACE:=insflow-demo}"
: "${IF_INGEST_SECRET:?请设置 IF_INGEST_SECRET（与服务端 INSFLOW_INGEST_SECRET 相同）}"

BODY=$(python3 - <<'PY'
import json
print(json.dumps({
    "schema_version": "1.0",
    "event": "cdp.order",
    "event_id": "ex-order-1",
    "source": "example",
    "workspace_id": "ignored-if-query-set",
    "data": {"identity": "buyer@example.com", "amount": 99.0, "channel": "search"},
}, ensure_ascii=False))
PY
)

SIG=$(printf '%s' "$BODY" | python3 -c "import hashlib,hmac,os,sys; print(hmac.new(os.environ['IF_INGEST_SECRET'].encode(), sys.stdin.buffer.read(), hashlib.sha256).hexdigest())")

curl -sS -X POST \
  -H "Content-Type: application/json" \
  -H "X-IF-Signature: $SIG" \
  --data "$BODY" \
  "$IF_BASE/api/v1/ingest?workspace_id=$IF_WORKSPACE"
echo
