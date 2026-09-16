# Insight Flow v1.0.0 发布说明

> 首个商业版本（2026-09-16）。自 M0 起六个里程碑迭代交付，首个付费客户已签约。

## 版本亮点

### 完整的"数据 → 洞察 → 行动 → 验证"闭环

从数据采集到效果验证全链路可用：**采集（12 个数据源插件）→ 建模（8 个内置模型 + 自定义 DSL）→ 洞察（质量门把关，证据+动作强制携带）→ 执行（6 个动作适配器落地到 OpenFlow/MFlow/飞书/Slack）→ 14 天窗口自动验证（验证报告反馈模型效果）**。

### 四类无人值守监控

| 监控类型 | 数据源 | 产出 |
|---------|--------|------|
| 网站变更 | Firecrawl 抓取定价页/Changelog | 价格异动 + 内容大改语义 diff 洞察 |
| 关键词机会 | GSC query 维度 | 高曝光低 CTR 捡漏清单 |
| 品牌提及 | GDELT 全球新闻 | 讨论量时序 + 舆情信号 |
| 客户旅程 | GA4 漏斗事件 | 相邻步断点 + 流失预警 |

cron 驱动、任务持久化、服务重启自动恢复。

### 三个可操作入口

- **Web 控制台**（`/console`）：仪表盘、接入向导、洞察流、监控、报告中心、插件市场、套餐用量——零构建链，SSR 直读数据
- **REST API**：OpenAPI 3.1（`/api/v1/openapi.json`），33 条路径，可直接导入 n8n/Dify/Postman
- **CLI**：`insflow run diagnosis / agent ask / report weekly / export`（JSON/CSV/MD 防锁定导出）

### 双向集成（家族生态优先）

- **OpenFlow**（执行操作系统）：零插件通道（洞察落 CDP）+ 官方插件 v1（7 个业务钩子出站、洞察回读 API、设置页）
- **MFlow**（内容流水线）：`mflow.create_content` 一键产稿（绝不代用户发布）、发布 webhook 回流进验证状态机
- **MCP Server**：10 个工具（stdio + HTTP 双传输），OpenFlow AgentRuntime / Claude / Cursor / Dify 均可消费

### 定制化能力

- **自定义规则模型 DSL**：运营配置 JSON 即可产出洞察模型（latest/any/trend 三窗口），静态校验兜底
- **模型效果追踪**：按模型聚合洞察命中率，驱动权重调优
- **插件市场 + Skill 市场**：本地目录安装，静态校验器把关（明文密钥扫描），未过检不进调度器
- **6 个 Agent Skill**：竞品异动/流量归因/市场之声/旅程断点/增长实验/周报撰写

### 商业化底座

- 4 档套餐（Free/Growth/Scale/Enterprise）+ 配额产品化（80% 预警 → 100% 拒绝）
- 白标报告（代理场景品牌化交付，自包含 HTML 可打印 PDF）
- 私有化部署包（Docker Compose / systemd / 自动回滚升级脚本）
- RBAC 四角色 + API Key 多 Key 认证 + 全量审计

---

## 升级与部署注意事项

1. **必填环境变量**：`INSFLOW_MASTER_KEY`（未设置则凭据保险库 fail-closed，无法保存任何 API Key）
2. **Python ≥ 3.12**（服务器自带 3.6 的环境需用 `/root/.local/bin/python3.12` 建虚拟环境）
3. **cryptography 50+** 兼容已内置（PBKDF2HMAC 命名自适应）
4. **Google OAuth** 需预先在 Google Cloud 配置 `GOOGLE_OAUTH_CLIENT_ID/SECRET` + `INSFLOW_BASE_URL`，回调路径为 `/api/v1/onboarding/oauth/callback`
5. **GDELT 免费源**有速率限制（数据中心 IP 易遇 429），建议监控 cron 错峰设置
6. **升级通道**：`./deploy/upgrade.sh <version>`（自动备份 data/，健康检查失败自动回滚）
7. **调度器**：默认随服务启动（监控恢复/验证评估/周报/配额审计）；`INSFLOW_DISABLE_SCHEDULER=1` 可关闭

## 已知边界（透明声明）

- GA4/GSC OAuth 的 refresh_token 轮换为手动（access_token 过期需重新授权或手动粘贴）
- journey/keyword 监控依赖第一方授权后的配额（GSC 1200 QPM / GA4 200k tokens 每天）
- 多租户 RBAC 为单库单 Workspace 级隔离，SaaS 多租户计费闭环待实际运营数据校准
