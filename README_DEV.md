# Insight Flow (insFlow)

增长情报与策略操作系统：数据采集 → 洞察生成 → 策略推荐 → 执行落地 → 效果回流

## 快速开始

### 安装

```bash
# 克隆仓库
git clone <repo-url>
cd "inFlow Dev"

# 创建虚拟环境
python -m venv .venv
source .venv/bin/activate  # macOS/Linux
# .venv\Scripts\activate   # Windows

# 安装依赖
pip install -e ".[dev]"
```

### 配置

```bash
# 复制配置文件
cp .env.example .env

# 编辑 .env 填入必要的 API Keys
```

### 初始化

```bash
# 初始化数据目录
insflow init

# 启动服务
insflow serve --reload
```

访问 http://localhost:8400 查看 API 文档

## CLI 命令

```bash
# 工作区管理
insflow workspace list
insflow workspace create "我的工作区"

# 洞察管理
insflow insight list -w <workspace-id>

# 插件检查
insflow plugin check plugins/sources/serper

# 启动服务
insflow serve [--port 8400] [--reload]
```

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /api/v1/workspaces | 列出工作区 |
| POST | /api/v1/workspaces | 创建工作区 |
| GET | /api/v1/insights | 列出洞察 |
| POST | /api/v1/insights | 创建洞察 |
| POST | /api/v1/insights/{id}/ack | 确认洞察 |
| POST | /api/v1/insights/{id}/dismiss | 忽略洞察 |
| POST | /api/v1/diagnosis/run | 触发诊断 |
| GET | /api/v1/maturity | 获取成熟度 |

## 开发

```bash
# 运行测试
pytest

# 代码检查
ruff check .

# 类型检查
mypy insflow/
```

## 目录结构

```
inFlow Dev/
├── insflow/              # 核心包
│   ├── cli.py           # CLI 入口
│   ├── core/            # 核心实体和存储
│   ├── server/          # FastAPI 应用
│   ├── collectors/      # 数据采集器
│   ├── engine/          # 洞察引擎
│   ├── agent/           # AI Agent
│   ├── actions/         # 动作执行
│   ├── integrations/    # 外部集成
│   └── mcp_server/      # MCP 服务器
├── plugins/             # 插件目录
├── skills/              # Agent Skills
├── data/                # 运行时数据
├── tests/               # 测试
├── pyproject.toml       # 项目配置
└── .env.example         # 环境变量示例
```

## 许可证

MIT
