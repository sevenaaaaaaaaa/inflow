#!/usr/bin/env bash
# 拉取事件目录（公开只读，无需 Key）。
set -euo pipefail
: "${IF_BASE:=http://127.0.0.1:8400}"
curl -sS "$IF_BASE/api/v1/events/catalog${1:+?direction=$1}"
echo
