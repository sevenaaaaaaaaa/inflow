#!/usr/bin/env bash
# 同步代码到服务器（唯一推荐入口，避免手写 rsync 漏排除项）
# 用法：
#   ./deploy/sync.sh            # 同步 + 重启 + 健康检查
#   ./deploy/sync.sh --dry-run  # 只看差异，不动线上
#
# 排除项说明（踩过的坑）：
#   /data/      仓库根 data（本机数据），注意必须锚定根目录——否则会连带排除 insflow/data/（内置地图）
#   .ruff_cache .pytest_cache __pycache__ 本机工具缓存，不该上服务器
#   .venv .env  服务器有自己的环境与配置
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE="${REMOTE:-nownexts:/www/wwwroot/inflow/}"
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

cd "$APP_DIR"
EXCLUDES=(
  --exclude='.git' --exclude='.venv' --exclude='__pycache__'
  --exclude='.pytest_cache' --exclude='.ruff_cache' --exclude='.mypy_cache'
  --exclude='/data/' --exclude='/data-backup/' --exclude='*.pyc'
  --exclude='.env' --exclude='.DS_Store'
)

if [ "$DRY" = "1" ]; then
  echo "==> 差异（dry-run）"
  rsync -azn --itemize-changes "${EXCLUDES[@]}" ./ "$REMOTE" || true
  echo "==> 仅预览，未变更线上"
  exit 0
fi

echo "==> 同步到 $REMOTE"
rsync -az "${EXCLUDES[@]}" ./ "$REMOTE"

echo "==> 重启服务"
ssh "${REMOTE%%:*}" "systemctl restart insflow && sleep 5 && systemctl is-active insflow"

echo "==> 健康检查"
BASE="${BASE:-https://www.nownexts.com/inflow}"
code=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/console/login")
echo "    /console/login -> $code"
[ "$code" = "200" ] || { echo "健康检查失败"; exit 1; }

echo "==> 版本核对（本地 vs 服务器）"
LOCAL_V="$(.venv/bin/python -c 'import sys; sys.path.insert(0,"."); from insflow.web.routes import ASSET_VERSION; print(ASSET_VERSION)' 2>/dev/null || echo '?')"
REMOTE_V="$(ssh "${REMOTE%%:*}" "cd /www/wwwroot/inflow && timeout 60 .venv/bin/python -c 'import sys; sys.path.insert(0,\".\"); from insflow.web.routes import ASSET_VERSION; print(ASSET_VERSION)' 2>/dev/null | tail -1" || echo '?')"
echo "    ASSET_VERSION 本地=$LOCAL_V 服务器=$REMOTE_V"
[ "$LOCAL_V" = "$REMOTE_V" ] || { echo "资源版本不一致（前端可能仍是旧缓存）"; exit 1; }
echo "==> 完成"
