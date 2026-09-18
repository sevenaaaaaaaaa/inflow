#!/usr/bin/env bash
# Insight Flow 一键装（私有化）：环境体检 → 虚拟环境 → 依赖 → 数据迁移 → 首跑体检
# 用法：./deploy/install.sh [--with-systemd] [--demo]
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
WITH_SYSTEMD=0
WITH_DEMO=0
for arg in "$@"; do
  case "$arg" in
    --with-systemd) WITH_SYSTEMD=1 ;;
    --demo) WITH_DEMO=1 ;;
    *) echo "未知参数：$arg（可用 --with-systemd --demo）"; exit 2 ;;
  esac
done

cd "$APP_DIR"
echo "==> 1/6 环境检查"
command -v python3 >/dev/null || { echo "需要 python3（3.11+）"; exit 1; }
PYVER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
echo "    python3 = $PYVER"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || {
  echo "    python 版本过低（需 ≥3.11）"; exit 1; }

echo "==> 2/6 配置 .env"
if [ ! -f .env ]; then
  cp .env.example .env 2>/dev/null || cat > .env <<'ENVEOF'
# Insight Flow 最小配置（按需补充数据源凭据）
INSFLOW_DB_DRIVER=sqlite
INSFLOW_MASTER_KEY=change-me-please
ENVEOF
  echo "    已生成 .env（请按需填写数据源凭据）"
else
  echo "    .env 已存在，跳过"
fi

echo "==> 3/6 虚拟环境与依赖"
[ -d .venv ] || python3 -m venv .venv
# shellcheck disable=SC1091
. .venv/bin/activate
pip install -q --upgrade pip
[ -f requirements.txt ] && pip install -q -r requirements.txt
echo "    依赖安装完成"

echo "==> 4/6 数据库迁移"
python -m insflow.cli db migrate

echo "==> 5/6 首跑体检（doctor）"
python -m insflow.cli doctor || echo "    doctor 有告警，请按提示修复"

if [ "$WITH_DEMO" = "1" ]; then
  echo "==> 可选：生成演示数据（便于看驾驶舱效果）"
  python -m insflow.cli demo seed -w default --days 30
fi

if [ "$WITH_SYSTEMD" = "1" ]; then
  echo "==> 可选：安装 systemd 服务"
  sudo cp deploy/systemd/insflow.service /etc/systemd/system/insflow.service
  sudo systemctl daemon-reload
  sudo systemctl enable --now insflow
  systemctl is-active insflow
fi

echo "==> 6/6 完成"
cat <<'TIP'
下一步：
  1) 配置数据源凭据：编辑 .env（GSC/GA4/SerpApi 等）或走控制台「接入向导」
  2) 启动服务：uvicorn insflow.server.app:app --host 0.0.0.0 --port 8400
     （或用 systemd：systemctl start insflow）
  3) 应用行业模板：insflow template list && insflow template apply <id> -w <workspace>
  4) 恢复演练：./deploy/restore-drill.sh
TIP
