# CLAUDE.md — Insight Flow (insFlow)

## 项目简介

Insight Flow（包名 `insflow`）：增长情报与策略操作系统。数据采集 → 洞察生成 → 策略推荐 → 执行落地 → 效果回流。提供 FastAPI 服务 + Web 控制台（`/console`）+ CLI（`insflow`）+ MCP Server，支持插件与 Agent Skills。

## 技术栈

- Python ≥ 3.12，hatchling 打包，依赖在 `pyproject.toml`（pip + `.venv`，无锁文件）
- FastAPI + uvicorn + Jinja2 + pydantic-settings + APScheduler + httpx + click + mcp
- 存储：SQLite（aiosqlite，默认）/ MySQL 5.7（可选 `.[mysql]`，pymysql）
- 前端交互：`insflow/web/static_app.js`（静态 JS 是交互唯一源，禁止内联到模板）
- 开发工具：pytest（+ asyncio、cov）、ruff、mypy

## 目录结构

```
insflow/            核心包：cli.py、core/（实体/存储/配置）、server/（FastAPI app）、web/（控制台）、
                    collectors/、engine/、agent/、actions/、integrations/、mcp_server/、viz/
tests/              pytest 测试（tests/e2e 为端到端冒烟）
scripts/            export_openapi.py（契约锁定）、e2e_ui.mjs（UI 回归）、smoke_live.sh（线上冒烟）
deploy/             sync.sh（同步上线）、install.sh、upgrade.sh、docker/、systemd/
contracts/          OpenAPI 契约
prompts/            Prompt 版本（<name>.v<N>.md）与 golden set
plugins/ skills/    插件与 Agent Skills
data/               运行时数据（不入库、不同步）
```

## 常用命令

```bash
# 安装
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # 需要 MySQL 时加 ,mysql

# 初始化与开发
insflow init
insflow serve --reload           # 默认 http://localhost:8400

# Lint / 类型检查
python -m ruff check insflow tests scripts
mypy insflow/

# 测试（与 CI 一致）
pytest -q -m "not e2e"
pytest tests/e2e -q
python scripts/export_openapi.py --check     # OpenAPI 契约锁定
node scripts/e2e_ui.mjs insflow/web/static_app.js   # UI 行为回归
```

构建：无独立构建步骤（Docker 镜像见下）。

## 部署方式

- 线上：自有服务器，ssh 别名 `nownexts`，路径 `/www/wwwroot/inflow/`，systemd 服务 `insflow`（监听 127.0.0.1:8400，反代到 `https://www.nownexts.com/inflow`）
- **双轨同步，唯一入口 `deploy/sync.sh`**：
  - GitHub 轨（公开仓库）：只推干净代码。`.gitignore` 挡住 `.env*`、`/data/`、`/Temp/` 等；`git add -A` 后 gitleaks 扫描暂存区，发现泄漏即中止；只推 `main`
  - 服务器轨：rsync 工作区（不带 --delete，排除 `.env`/`.venv`/`/data/` 等）→ `systemctl restart insflow` → 健康检查 `/console/login` → 核对 `ASSET_VERSION`
  - 只允许从 `main` 部署（含 `--server-only`）；其他分支直接拒绝，`--dry-run` 仅告警
  - `ops/` 单独同步到 `/www/ops/inflow/`（见「开发工具 vs 使用工具」）
  - 本地 `.git/hooks/pre-commit` 跑 gitleaks（不入库，新 clone 需自行安装）；误报登记在 `.gitleaksignore` 或行内 `gitleaks:allow`

```bash
./deploy/sync.sh                     # gitleaks → commit → push main → 同步 → 重启 → 健康检查
./deploy/sync.sh -m "feat: 说明"      # 同上，指定提交信息（默认 chore: sync）
./deploy/sync.sh --dry-run           # 只看差异：gitleaks + rsync -n，不提交/不推送/不重启
./deploy/sync.sh --server-only       # 跳过 git，只发服务器（= 旧版默认行为）
BASE=https://www.nownexts.com/inflow EMAIL=... PASSWORD=... ./scripts/smoke_live.sh   # 部署后线上冒烟
```

- CI：`.github/workflows/ci.yml` 在 push `main` / PR 时跑单测、ruff、契约检查、MySQL 兼容、UI 回归；**不自动部署**
- 私有化形态：`deploy/install.sh`、`deploy/upgrade.sh`、`deploy/docker/`（`docker compose -f deploy/docker/docker-compose.yml --env-file .env up -d --build`）、`deploy/systemd/`

## 开发工具 vs 使用工具（硬性规则）

| 类别 | 例子 | 放哪 | GitHub | 服务器 |
|---|---|---|---|---|
| 开发工具（产品代码） | 源码、测试、部署脚本、文档 | 原目录 | ✅ | ✅（部署同步） |
| 使用工具的产物 | 用 inFlow 干活时写的批处理/一次性修复脚本、批次文件、导出数据 | **`ops/`** | ❌ | ✅ 仅 `/www/ops/inflow/`（web 根之外，`--update` 只增不删） |

- `/ops/` 已 gitignore；`deploy/sync.sh` 把 `ops/` 单独同步到服务器 `/www/ops/inflow/`（不进 web 根、不进 GitHub；`ops/` 为空时跳过）。
- 拿不准时按第二类处理（放 `ops/`，不进 GitHub），再问用户。

## 注意事项

- 配置从仓库根 `.env` 读取（参照 `.env.example`；不要读取或提交 `.env`）。`INSFLOW_MASTER_KEY` 必填（fail-closed）
- 环境变量（仅名称，主要）：`INSFLOW_MASTER_KEY`、`INSFLOW_DB_DRIVER`、`INSFLOW_DB_PATH`、`INSFLOW_DATA_DIR`、`INSFLOW_BASE_URL`、`INSFLOW_BASE_PATH`、`INSFLOW_DISABLE_SCHEDULER`、`INSFLOW_ADMIN_EMAIL`、`INSFLOW_API_AUTH`、`INSFLOW_MCP_AUTH`、`INSFLOW_WEBHOOK_SECRET`、`INSFLOW_INGEST_SECRET`、`INSFLOW_AGENT_MODEL*`、`INSFLOW_AGENT_BUDGET_USD`、`INSFLOW_EMBED_MODEL`、`INSFLOW_OIDC_*`、`INSFLOW_SCIM_TOKEN`、`INSFLOW_REDIS_URL`、`MYSQL_HOST/PORT/USER/PASSWORD/DATABASE`、`OPENAI_API_KEY`、`OPENAI_BASE_URL`、`GOOGLE_OAUTH_CLIENT_ID/SECRET`、`OPENFLOW_*`、`SMTP_HOST/PORT/USER/PASSWORD/FROM/TLS`
- 测试/CI 中可设 `INSFLOW_DISABLE_SCHEDULER=1` 避免定时任务启动
- 前端改动后需同步 `insflow/web/routes.py` 中的 `ASSET_VERSION`，sync.sh 会校验本地与线上一致
- rsync 排除 `/data/` 必须锚定根目录，否则会误排除 `insflow/data/`（内置地图）
