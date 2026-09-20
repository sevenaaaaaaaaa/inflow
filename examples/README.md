# Insight Flow 示例（可直接跑）

公共环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `IF_BASE` | `http://127.0.0.1:8400` | 服务根地址 |
| `IF_WORKSPACE` | `insflow-demo` | 工作区 ID（`insflow quickstart` 默认） |
| `IF_API_KEY` | （空） | 仅当 `INSFLOW_API_AUTH=1` 时需要 |
| `IF_INGEST_SECRET` | （空） | 入站 HMAC，对应 `INSFLOW_INGEST_SECRET` |

先起一份本地实例：

```bash
insflow quickstart --no-open
```

| 目录 | 做什么 |
|---|---|
| [curl/](curl/) | 列洞察、读事件目录、HMAC 入站 |
| [python/](python/) | 同上（仅标准库） |
| [typescript/](typescript/) | `npx tsx` 列洞察 |
| [n8n/](n8n/) | 周报自动分发工作流 |
| [dify/](dify/) | Agent 接 MCP HTTP |
| [mcp/](mcp/) | Claude Desktop / Cursor 配置 |

完整契约：`GET $IF_BASE/api/v1/openapi.json` 或仓库 `contracts/openapi.json`。
事件字段：`GET $IF_BASE/api/v1/events/catalog`。
