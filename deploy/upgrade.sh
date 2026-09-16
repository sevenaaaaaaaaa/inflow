#!/usr/bin/env bash
# Insight Flow 升级通道（私有化）
# 用法：./deploy/upgrade.sh [version|main]
# 流程：备份 data/ → 拉取指定版本 → 依赖安装 → 健康检查（失败自动回滚）

set -euo pipefail

TARGET="${1:-main}"
APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
BACKUP_DIR="$APP_DIR/data-backup"
LOG=/tmp/insflow-upgrade.log

echo "==> 目标版本: $TARGET"
cd "$APP_DIR"

# 1. 备份数据（SQLite WAL 需先 checkpoint）
echo "==> 备份 data/"
mkdir -p "$BACKUP_DIR"
python3 - <<EOF || sqlite3 "$APP_DIR/data/insflow.db" ".backup '$BACKUP_DIR/insflow-$(date +%Y%m%d-%H%M%S).db'"
EOF
cp -a "$APP_DIR/data" "$BACKUP_DIR/data-$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true

# 2. 拉取代码
echo "==> 拉取 $TARGET"
git fetch origin >>"$LOG" 2>&1
git checkout "$TARGET" >>"$LOG" 2>&1
git pull origin "$TARGET" >>"$LOG" 2>&1 || true

# 3. 依赖 + 服务重启
echo "==> 安装依赖"
source "$APP_DIR/.venv/bin/activate"
pip install -e . -q >>"$LOG" 2>&1

echo "==> 重启服务"
if systemctl is-active --quiet insflow; then
    systemctl restart insflow
else
    pkill -f "insflow serve" 2>/dev/null || true
    sleep 1
    setsid nohup insflow serve --host 127.0.0.1 --port 8400 >>"$APP_DIR/data/serve.log" 2>&1 &
fi

# 4. 健康检查 + 回滚
for i in $(seq 1 10); do
    sleep 2
    if curl -sf http://127.0.0.1:8400/health >/dev/null; then
        echo "✅ 升级完成，健康检查通过（版本 $(insflow --version 2>/dev/null | awk '{print $NF}')）"
        exit 0
    fi
done

echo "❌ 健康检查失败，回滚到升级前数据快照：$BACKUP_DIR"
LATEST_BACKUP=$(ls -dt "$BACKUP_DIR"/data-* 2>/dev/null | head -1)
if [ -n "$LATEST_BACKUP" ]; then
    rm -rf "$APP_DIR/data" && mv "$LATEST_BACKUP" "$APP_DIR/data"
fi
exit 1
