#!/usr/bin/env bash
# 线上冒烟（真实 HTTP）：登录 → 关键页面 → 关键 API（部署后跑一遍）
# 用法：BASE=https://host/inflow EMAIL=... PASSWORD=... ./scripts/smoke_live.sh
set -euo pipefail

BASE="${BASE:-https://www.nownexts.com/inflow}"
EMAIL="${EMAIL:?需要 EMAIL}"
PASSWORD="${PASSWORD:?需要 PASSWORD}"
WS="${WS:-insflow-main}"
JAR="$(mktemp)"
FAIL=0

hit() {  # hit <期望码> <说明> <curl参数...>
  local want="$1" label="$2"; shift 2
  local code
  code=$(curl -s -o /tmp/smoke.out -w "%{http_code}" "$@")
  if [ "$code" = "$want" ]; then echo "PASS $label ($code)"
  else echo "FAIL $label（期望 $want 得到 $code）"; FAIL=1; fi
}

echo "==> 登录"
hit 303 "login" -c "$JAR" -b "$JAR" -X POST "$BASE/console/login" \
  -d "email=$EMAIL&password=$PASSWORD&next=/console"

echo "==> 页面"
for path in "console" "console/cockpit/traffic?days=30" "console/cockpit/action-loop" \
            "console/explore?metric=ga4_sessions&chart=treemap" "console/m" \
            "console/audit?workspace_id=$WS"; do
  hit 200 "page $path" -b "$JAR" "$BASE/$path"
done

echo "==> API"
hit 200 "catalog"  -b "$JAR" "$BASE/api/v1/metrics/catalog?workspace_id=$WS"
hit 200 "dq"       -b "$JAR" "$BASE/api/v1/data-quality?workspace_id=$WS"
hit 200 "lift"     -b "$JAR" "$BASE/api/v1/attribution/lift?workspace_id=$WS"
hit 200 "obs"      -b "$JAR" "$BASE/api/v1/observability?workspace_id=$WS"
hit 200 "xlsx"     -b "$JAR" "$BASE/api/v1/export/xlsx?workspace_id=$WS&panel=cockpit:traffic&days=30"
hit 200 "pwa-sw"   "$BASE/console/sw.js"
hit 403 "embed-bad" "$BASE/console/embed?token=bad"
hit 401 "scim-closed" "$BASE/scim/v2/Users?workspace_id=$WS"

rm -f "$JAR"
[ "$FAIL" = "0" ] && echo "==> 冒烟通过" || { echo "==> 有失败项"; exit 1; }
