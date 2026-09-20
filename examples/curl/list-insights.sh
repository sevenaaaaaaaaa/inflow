#!/usr/bin/env bash
# 列出工作区洞察。
#   IF_BASE=http://127.0.0.1:8400 IF_WORKSPACE=insflow-demo ./list-insights.sh
set -euo pipefail
: "${IF_BASE:=http://127.0.0.1:8400}"
: "${IF_WORKSPACE:=insflow-demo}"

AUTH=()
if [ -n "${IF_API_KEY:-}" ]; then
  AUTH=(-H "X-API-Key: $IF_API_KEY")
fi

curl -sS "${AUTH[@]}" \
  "$IF_BASE/api/v1/insights?workspace_id=$IF_WORKSPACE&limit=5"
echo
