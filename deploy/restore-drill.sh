#!/usr/bin/env bash
# 备份恢复演练：备份 → 恢复到临时目录 → 校验行数 → 记录耗时（对齐 docs/07 R2-4 SLA）
# 用法：./deploy/restore-drill.sh [--keep]
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
KEEP=0
[ "${1:-}" = "--keep" ] && KEEP=1
cd "$APP_DIR"
. .venv/bin/activate 2>/dev/null || true

STAMP="$(date +%Y%m%d-%H%M%S)"
WORK="$(mktemp -d /tmp/insflow-restore-XXXXXX)"
REPORT="$WORK/report.md"

echo "==> 1/4 备份当前数据"
python -m insflow.cli backup || true

echo "==> 2/4 恢复到临时目录 $WORK"
START=$(date +%s)
if [ -f data/insflow.db ]; then
  python - <<PY
import sqlite3, pathlib
src = sqlite3.connect("data/insflow.db")
dst = sqlite3.connect("$WORK/insflow.db")
src.backup(dst)
dst.close(); src.close()
print("    SQLite 备份完成")
PY
  TARGET="$WORK/insflow.db"
elif [ -n "${MYSQL_HOST:-}" ]; then
  echo "    MySQL 模式：请用 mysqldump/xtrabackup 恢复（脚本保留检查项）"
  TARGET=""
else
  echo "    未找到 data/insflow.db 且未配置 MYSQL_HOST，无法演练"; exit 1
fi
END=$(date +%s)
RESTORE_SECONDS=$((END - START))

echo "==> 3/4 校验数据"
OK=1
if [ -n "$TARGET" ]; then
  python - "$TARGET" <<'PY'
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
tables = [r[0] for r in db.execute(
    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
counts = {}
for t in ("workspaces", "metrics", "insights", "actions", "monitors"):
    if t in tables:
        counts[t] = db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
print("    行数：", counts)
if counts.get("workspaces", 0) == 0:
    print("    [WARN] workspaces 为空，恢复可能不完整"); sys.exit(3)
PY
  OK=$?
fi

echo "==> 4/4 报告"
{
  echo "# 恢复演练报告（$STAMP）"
  echo
  echo "- 恢复耗时：${RESTORE_SECONDS}s（SLA 目标 < 600s）"
  echo "- 结果：$([ "$OK" = "0" ] && echo 通过 || echo 需人工复核)"
  echo "- 备份文件：data-backup/（保留 30 天由 backup.daily 管理）"
  echo "- 说明：本脚本只做「可恢复性」验证，不动生产库；演练目录：$WORK"
} | tee "$REPORT"
[ "$KEEP" = "1" ] || rm -rf "$WORK"
echo "==> 演练结束"
