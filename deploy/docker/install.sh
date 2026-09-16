#!/usr/bin/env bash
# Insight Flow 一键部署（R3：docker compose 一条命令）
# 用法：./deploy/docker/install.sh [--port 8400] [--pack saas-growth]
#
# 流程：校验环境 → 生成主密钥（如缺）→ 构建启动 → 等健康检查 → 输出接入向导链接

set -euo pipefail

PORT=8400
PACK="saas-growth"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --pack) PACK="$2"; shift 2 ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
done

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

echo "==> Insight Flow 一键部署（端口 $PORT，模板 $PACK）"

# 1. 环境校验
command -v docker >/dev/null 2>&1 || { echo "❌ 需要 Docker"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "❌ 需要 Docker Compose v2"; exit 1; }

# 2. .env 与主密钥（fail-closed：没有主密钥凭据保险库不可用）
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "==> 已从 .env.example 创建 .env"
fi
if ! grep -q '^INSFLOW_MASTER_KEY=.\+' .env; then
  MASTER_KEY="$(openssl rand -hex 32 2>/dev/null || head -c 32 /dev/urandom | xxd -p | tr -d '\n')"
  if grep -q '^INSFLOW_MASTER_KEY=' .env; then
    sed -i.bak "s|^INSFLOW_MASTER_KEY=.*|INSFLOW_MASTER_KEY=${MASTER_KEY}|" .env
  else
    echo "INSFLOW_MASTER_KEY=${MASTER_KEY}" >> .env
  fi
  echo "==> 已生成主密钥并写入 .env（请妥善保管，丢失则凭据不可解密）"
fi

# 3. 构建并启动
echo "==> 构建镜像并启动服务"
docker compose -f deploy/docker/docker-compose.yml --env-file .env up -d --build

# 4. 等健康检查（最多 60s）
echo -n "==> 等待服务就绪"
for i in $(seq 1 30); do
  if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    echo " ✅"
    break
  fi
  echo -n "."
  sleep 2
  if [[ $i -eq 30 ]]; then
    echo ""
    echo "❌ 服务未在 60s 内就绪，查看日志：docker compose -f deploy/docker/docker-compose.yml logs"
    exit 1
  fi
done

# 5. 引导：建 workspace + 应用模板
echo "==> 初始化工作区并应用行业模板"
docker compose -f deploy/docker/docker-compose.yml exec -T insflow \
  insflow setup --name "首个工作区" --pack "$PACK" --base-url "http://127.0.0.1:${PORT}" || true

VERSION="$(curl -s "http://127.0.0.1:${PORT}/health" | sed -n 's/.*"version":"\([^"]*\)".*/\1/p')"
echo ""
echo "✅ Insight Flow ${VERSION} 部署完成"
echo "   控制台：      http://127.0.0.1:${PORT}/console"
echo "   接入向导：    http://127.0.0.1:${PORT}/console/onboarding"
echo "   体检：        docker compose -f deploy/docker/docker-compose.yml exec insflow insflow doctor"
echo "   API 文档：    http://127.0.0.1:${PORT}/api/v1/docs"
echo ""
echo "   下一步：完成第一方数据接入（GSC/GA4/CrUX）后触发首诊"
