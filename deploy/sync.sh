#!/usr/bin/env bash
# 双轨同步（唯一推荐入口，避免手写 rsync 漏排除项）
#   GitHub 轨（公开仓库）：只推干净代码 —— .gitignore 挡住 .env / data/ / Temp/ 等，gitleaks 扫描把关
#   服务器轨：rsync 工作区（不带 --delete），排除服务器自有的数据与环境
# 用法：
#   ./deploy/sync.sh                     # gitleaks → git add -A + commit + push main → 同步 → 重启 → 健康检查
#   ./deploy/sync.sh -m "feat: xxx"      # 同上，指定提交信息（默认 chore: sync）
#   ./deploy/sync.sh --server-only       # 跳过 git，只同步服务器 + 重启 + 健康检查（即旧版默认行为）
#   ./deploy/sync.sh --dry-run           # 只看差异：gitleaks 扫描 + rsync -n，不提交/不推送/不重启
#
# 排除项说明（踩过的坑）：
#   /data/      仓库根 data（本机数据），注意必须锚定根目录——否则会连带排除 insflow/data/（内置地图）
#   .ruff_cache .pytest_cache __pycache__ 本机工具缓存，不该上服务器
#   .venv .env  服务器有自己的环境与配置
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE="${REMOTE:-nownexts:/www/wwwroot/inflow/}"
DRY=0; SERVER_ONLY=0; MSG="chore: sync"
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)      DRY=1 ;;
    --server-only)  SERVER_ONLY=1 ;;
    -m|--message)   shift; MSG="${1:?-m 需要提交信息}" ;;
    -h|--help)      sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "未知参数: $1（见 --help）" >&2; exit 2 ;;
  esac
  shift
done

cd "$APP_DIR"
EXCLUDES=(
  --exclude='.git' --exclude='.venv' --exclude='__pycache__'
  --exclude='.pytest_cache' --exclude='.ruff_cache' --exclude='.mypy_cache'
  --exclude='/data/' --exclude='/data-backup/' --exclude='*.pyc'
  --exclude='.env' --exclude='.env.*' --exclude='.DS_Store'
  --exclude='/Temp/' --exclude='CLAUDE.md' --exclude='AGENTS.md' --exclude='.gitleaksignore'
)

GITLEAKS="${GITLEAKS:-$HOME/.local/bin/gitleaks}"
[ -x "$GITLEAKS" ] || GITLEAKS="$(command -v gitleaks || true)"
leak_scan() {  # $1 = staged | worktree
  [ -n "$GITLEAKS" ] || { echo "✗ 未找到 gitleaks，拒绝提交（或用 --server-only）"; return 1; }
  local args=(git --pre-commit --redact --no-banner); [ "$1" = staged ] && args+=(--staged)
  "$GITLEAKS" "${args[@]}" . || {
    echo "✗ gitleaks 发现疑似密钥，已中止（未提交/未推送/未同步）。误报请加 .gitleaksignore 或行内 gitleaks:allow"
    return 1
  }
}

# ---------- GitHub 轨 ----------
if [ "$SERVER_ONLY" = "0" ]; then
  if [ "$DRY" = "1" ]; then
    echo "==> [dry-run] 待提交改动"
    git status -s | head -40
    echo "==> [dry-run] gitleaks 扫描工作区改动"
    leak_scan worktree
    echo "    将提交：${MSG}（dry-run 不提交、不推送）"
  else
    br="$(git rev-parse --abbrev-ref HEAD)"
    [ "$br" = "main" ] || { echo "✗ 当前分支 $br 不是 main，已中止（或用 --server-only）"; exit 1; }
    git add -A
    if git diff --cached --quiet; then
      echo "==> GitHub：没有改动，跳过提交"
    else
      echo "==> gitleaks 扫描暂存区"
      leak_scan staged
      git --no-pager diff --cached --stat | tail -15
      git commit -q -m "$MSG"
    fi
    echo "==> git push origin main"
    git push -q origin main
  fi
fi

# ---------- 服务器轨 ----------
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
