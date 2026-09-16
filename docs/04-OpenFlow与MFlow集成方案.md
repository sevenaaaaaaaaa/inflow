# Insight Flow × OpenFlow × MFlow 集成方案

> 本方案基于对本机两个系统的**真实代码架构调研**（2026-09-16），所有集成点均对应实际存在的机制，非设想接口。
>
> - **OpenFlow XMP v2.5**（`~/OpenFlowDev`）：PHP 8 增长操作系统，TIPS 模型（Touch/Insight/Personalize/Sell）。已具备：插件平台 v2（钩子/API 路由/MCP 工具注册）、通用连接器（OAuth2+PKCE）、Skill 系统、MCP Server（ApiKeyAuth + McpGuard 逐工具审计）、CDP 数据中枢、GrowthBrain、AgentRuntime（白名单工具 + 审批门）、出站 Webhook（HMAC + 死信重试）、入站 InboundReceiver。
> - **MFlow v1.0.0**（`~/MFlow Dev`）：Python 内容营销流水线，"文件 + 状态机 + 定时任务"哲学。已具备：三类插件（source/publisher/template，manifest.json + entry.py + plugin_check.py 六项校验）、HTTP 控制台（console.py，`/api/loop/create` 异步产稿）、出站 webhook 适配器、`$LOVART_LOCAL_DEV_ROOT/Output/Data Ingestion/` 标准数据入口、`1-2 Insight/` 情报资产目录。
>
> **总原则**：零改两个系统的内核代码，全部走官方扩展点；优先插件、次选标准 API。

---

## 1. 集成全景

```
                    ┌─────────────────────────────────────┐
                    │           Insight Flow              │
                    └───┬─────────┬─────────┬─────────┬───┘
        ①事件流入(出站)  │         │         │         │  ③洞察回读/工具
   ┌────────────────────┘    ②动作派发  │    ④MCP 工具      │
   │                    │         │    │         │         │
   ▼                    ▼         ▼    ▼         ▼         ▼
┌────────────── OpenFlow ─────────┐   ┌─────────── MFlow ──────────┐
│ plugins/insight-flow/ (官方插件) │   │ plugins/insight-flow/      │
│  · 监听 cdp_event_received 等    │   │  (type: source 官方插件)    │
│    钩子→推送 IF (事件流入)        │   │  · collect() 每日拉选题包   │
│  · register_api_route 洞察回读   │   │    → Output/Data Ingestion │
│  · register_mcp_tool Agent 消费  │   │  · 反向: POST /api/loop/   │
│  · 连接器: 画布/Agent 直调 IF    │   │    create 触发产稿+轮询     │
│  · IF → POST /api/webhook.php   │   │  · 发布 webhook 回流到 IF  │
│    (InboundReceiver, 洞察落 CDP) │   │    (run/cms.json 配置)     │
│  · IF 作 MCP 客户端读 OF 数据    │   │  · 报告落 1-2 Insight/     │
└──────────────────────────────────┘   └────────────────────────────┘
```

四条数据通道：**① 行为/成交事件流入**（OpenFlow→IF，喂养旅程与 LTV 模型）；**② 洞察→执行动作派发**（IF→MFlow/OpenFlow）；**③ 洞察回读**（OpenFlow 后台/Agent 随查随用）；**④ 效果回流**（MFlow 发布结果、OpenFlow 转化数据回流 IF 验证洞察）。

---

## 2. 与 MFlow 的集成（内容落地闭环）

### 2.1 Insight Flow 作为 MFlow 的数据源插件（P0，首选路径）

MFlow 的插件机制（`docs/plugins.md` + `plugins/plugin_check.py`）原生支持 `source` 类型。在 **MFlow 仓库**新建 `plugins/insight-flow/`：

**manifest.json**（严格对齐 MFlow 规范，参考 `plugins/sample-source/manifest.json`）：

```json
{
  "id": "insight-flow",
  "type": "source",
  "name": "Insight Flow 每日洞察选题包",
  "version": "1.0.0",
  "entry": "entry.py",
  "permissions": ["network", "credentials:insight-flow"],
  "config": {
    "base_url": {"desc": "Insight Flow API 地址", "required": false},
    "api_key": {"desc": "IF API Key", "required": true, "secret": true},
    "workspace": {"desc": "IF 工作区 ID", "required": true},
    "min_severity": {"desc": "最低洞察级别 watch", "required": false}
  }
}
```

**entry.py**（实现 MFlow 约定的 `collect(cfg)`）：

```python
def collect(cfg=None):
    """拉取 Insight Flow 洞察，转成 MFlow 选题格式。
    落点：$LOVART_LOCAL_DEV_ROOT/Output/Data Ingestion/（与 GSC/Sentinel 同等数据流），
    之后自动进入 MFlow 的信号选题 → 路由器派工 → 生成环节。"""
    # GET {base_url}/api/v1/insights?status=new&min_severity=watch&intent=content
    #   → 每条洞察映射为 MFlow 选题条目：
    #     topic（洞察标题）、brief（summary+evidence 摘要）、
    #     keywords（关联搜索需求词）、source_ref（insight id，用于回流追踪）
    # 原子写 JSON 到 Data Ingestion 目录，返回 {"source": "insight-flow", "date": ..., "count": n, "items": [...]}
```

要点：
- 密钥走 MFlow 凭据目录（`permissions: ["credentials:insight-flow"]`），**不得明文**（plugin_check 会拦）；
- 路径含空格：脚本内一律用 `$LOVART_LOCAL_DEV_ROOT` 环境变量推导，不硬编码；
- 上线前跑 `python3 plugins/plugin_check.py plugins/insight-flow` 过六项校验。

### 2.2 Insight Flow 反向触发 MFlow 产稿（P0，Action 适配器 `mflow.create_content`）

IF 的 Action Router 内置 `mflow.*` 动作族，调用 MFlow 控制台 HTTP API（`1-4 Dev/console/console.py`）：

```
认证：POST /api/login（密码换 session cookie；凭据存 IF 保险库）
派发：POST /api/loop/create  {item_id?, topic, brief, template_id?, max_rounds:3}
      → 立即返回 {ok, id, queued}（异步，对齐 MFlow P5 排程器）
轮询：GET /api/loop/detail?id=...  → status: queued|running|done
取稿：GET /api/read?path=...（白名单扩展名+项目内路径）→ 草稿 Markdown
状态同步：POST /api/item/upsert + /api/item/advance（把该选题登记进 MFlow 12 阶段状态机）
```

设计约束：
- **发布永远停在人工授权后**（MFlow 铁律）——IF 只创建 Loop 与草稿，绝不替用户点"发布"；
- 同步/异步双模式：交互场景用 `POST /api/generate` 同步取稿（快但阻塞），批量场景用 loop/create + 轮询；
- 模板联动：IF 洞察的 `stage_tags` 映射 MFlow `templates/` 模板包（如 saas-growth.json），生成时经 `gen_prompt()` 应用。

### 2.3 发布结果回流（P1，闭环的"最后一公里"）

MFlow 的 `1-4 Dev/scripts/publish_adapters/webhook.py` 是现成的**出站**适配器（POST item JSON 到配置 URL，返回体含 `{url}` 即回写）。在 MFlow 项目 `run/cms.json` 配置：

```json
{"webhook": {"url": "https://<if-host>/api/v1/ingest", "secret": "<hmac-secret>"}}
```

IF 侧 `/api/v1/ingest`（HMAC 验签）接收 `content.published` 事件 → 关联该内容对应的 insight id → 进入动作验证状态机（14 天窗口）→ 拉取 GSC 排名/GA4 流量对比基线 → 产出《选题假设验证报告》反馈给模型权重。

### 2.4 报告与知识挂载（P2）

- IF 周报（竞品动向/流量诊断）经 source 插件同步落 MFlow `1-2 Insight/` **独立子目录**（`1-2 Insight/Insight Flow Reports/`，不与现有 SEO 报告混目录），自动出现在 MFlow 报告中心（console.py REPORT_CATS 机制）；
- 若 IF 输出长期知识（如"竞品定价史"），经 `POST /api/projects/config` 的 `meta.kb_extra` 挂进 MFlow 知识中台，成为生成时的 grounded context。

---

## 3. 与 OpenFlow 的集成（执行操作系统闭环）

### 3.1 官方插件 `plugins/insight-flow/`（P0，双向通道）

在 **OpenFlow 仓库**新建插件（结构照抄 `plugins/deal-notifier/` 示例 + `plugin.json` 声明权限）：

```json
{
  "id": "insight-flow",
  "name": "Insight Flow 增长情报",
  "version": "1.0.0",
  "permissions": ["hooks", "config", "log", "http", "api", "schedule", "mcp"]
}
```

**出站钩子（行为事件流入 IF）**——照抄 deal-notifier 的 5 秒超时旁路写法，绝不阻塞主请求：

| OpenFlow 钩子（已存在） | 推送给 IF 的意义 |
|---|---|
| `cdp_event_received`（filter，唯一能丢数据的钩子） | 实时行为流 → IF 客户旅程重建、转化漏斗 |
| `cdp_segment_enter` / `cdp_segment_exit` | 分群迁移事件 → 生命周期阶段分析 |
| `payment_success` / `crm_deal_won` | 成交真相 → LTV:CAC 模型、RFM 分层 |
| `form_submitted` / `user_registered` | 获客漏斗 S1 诊断数据 |

→ 统一 POST 到 `IF /api/v1/ingest`（HMAC 签名，格式参照 `lib/InboundReceiver.php`）。

**入站 API（洞察回读）**——`PluginSystem::register_api_route` 注册：

```
GET /api/plugin/insight-flow/insights?severity=warning&limit=10   # 后台卡片数据源
GET /api/plugin/insight-flow/insights/{id}                        # 详情含 evidence
POST /api/plugin/insight-flow/feedback                             # 动作执行结果回传 IF
```

（插件路由默认 admin 档鉴权，符合 OpenFlow ApiPolicy 白名单机制，不新增公共端点。）

**后台集成面**：`register_admin_page` 设置页（IF 地址/API Key/订阅哪些洞察类型）；Dashboard 挂"今日增长情报"卡片；GrowthBrain 侧把 IF 洞察作为"下一最佳动作"的外部信号源读取该 API。

**定时同步**：`register_schedule` 每日拉取 IF 周报摘要写进 OpenFlow 报告/通知流。

**MCP 工具注册（Agent 消费）**：`PluginSystem::register_mcp_tool`（`lib/PluginSystem.php:651`）注册 `insight_flow_query` → OpenFlow AgentRuntime 的 `mcp:*` 工具通道即可在自主 Loop 中调用"查竞品异动/查流量诊断"，经其风险审批门后把洞察写回（如 `create_flow`/`update_lead`）。

### 3.2 零插件快速通道（P0 并行，当天可跑通）

不改 OpenFlow 代码：IF 洞察直接推 `POST /api/webhook.php`（InboundReceiver，`X-Inbound-Signature` = HMAC-SHA256(rawBody, secret)，类型 `lead`/`cdp_event`/`contact`）→ 情报立即落 CDP 画像/线索 → 被 GrowthBrain、分群、自动化画布全链路消费。适合验证期。

### 3.3 连接器反向调用（P1）

在 OpenFlow 后台 `admin/connections.php` 建一条到 IF 的连接（api_key/bearer/OAuth2 均支持，凭据经 `lib/Secrets.php` 加密）：自动化画布节点与 AgentRuntime（`conn:{id}` 工具）即可调用 IF REST API（如"成交后触发一次竞品对比诊断"）。IF 未来若开放 OAuth2 授权码模式，回调对接 OpenFlow `/api/oauth-callback.php`（标准 RFC 6749+PKCE）。

### 3.4 IF 作为 OpenFlow MCP 客户端（P1，喂饱第一方模型）

IF 内置 MCP 客户端连接 OpenFlow `mcp-server.php`（HTTP 传输 + ApiKeyAuth 多 Key + `McpGuard` 逐工具 scope/审计），消费其 18 个工具中的数据类工具（members_list、leads_count、orders_revenue、sentiment_* 等）→ 旅程重建（CJ-3）、RFM 分层（CJ-5）、LTV:CAC（IM-1）的第一方数据底座。**只读 scope**，写操作一律经 OpenFlow 自己的 Agent 审批门。

---

## 4. 动作路由器映射表（IF Action Router 内置适配器）

| action_type | 目标 | 调用 | 对应 PRD |
|---|---|---|---|
| `mflow.create_content` | MFlow | `/api/loop/create`（异步）或 `/api/generate`（同步） | AC-2 |
| `mflow.register_topic` | MFlow | `/api/item/upsert` + `advance` 状态机登记 | AC-2 |
| `openflow.webhook_insight` | OpenFlow | `POST /api/webhook.php`（InboundReceiver HMAC） | AC-1/3.2 |
| `openflow.plugin_api` | OpenFlow | `POST /api/plugin/insight-flow/*` | AC-1 |
| `openflow.automation` | OpenFlow | 连接器 `conn_request()`（画布自动化触发） | 3.3 |
| `webhook.generic` | n8n/Zapier/Make/Dify | 出站 Webhook（HMAC + 退避重试 + 死信） | AC-4 |
| `feishu.notify` | 飞书 | 自定义机器人 webhook（告警/日报） | PL-3 |

每个适配器实现统一接口：`execute(action, ctx) -> {ok, ref, detail}`，结果写 `actions` 表并可被验证状态机追踪。

---

## 5. 通用生态兼容（"既然是 Flow，必须开放"）

| 生态 | 接入方式 |
|---|---|
| **n8n / Zapier / Make** | 出站 Webhook（HMAC 签名，事件订阅）+ REST API + OpenAPI 3.1 规范文件（可直接导入 n8n 生成节点） |
| **Dify / Coze / LangChain** | MCP Server（工具清单见架构文档 §9.2）+ OpenAPI 工具导入 |
| **Claude / Cursor / 任意 MCP 客户端** | MCP stdio（本地）+ HTTP/SSE（远程，多 Key 认证） |
| **飞书 / Slack** | 通知出站；飞书经自研 lark-cli 生态可扩展为双向（P2：飞书卡片按钮回调 ack/dismiss 洞察） |
| **Grafana / 数据导出** | SQLite 只读连接 + `insflow export` 全量导出（JSON/CSV/MD），防锁定承诺 |

---

## 6. 实施顺序与验收

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| **W1–W2**（M0 内） | MFlow source 插件 + IF `/api/v1/insights` API | MFlow plugin_check 通过；MFlow 信号选题流中出现 IF 推送的选题条目 |
| **W2–W3** | OpenFlow 零插件通道：IF → webhook.php 落 CDP | OpenFlow CDP 画像中出现 insight 来源属性；GrowthBrain 可读取 |
| **M1** | OpenFlow 官方插件 v1（钩子出站 + 洞察回读路由 + 设置页） | cdp_event_received 事件 5 秒内到达 IF；OpenFlow 后台显示情报卡片 |
| **M2** | `mflow.create_content` 动作 + 发布 webhook 回流 + 验证状态机 | 一条洞察可一键生成 MFlow 草稿；发布 14 天后自动产出验证报告 |
| **M2** | IF MCP Server + OpenFlow `register_mcp_tool` | OpenFlow AgentRuntime Loop 中成功调用 insight_flow_query 并执行派生动作（经审批门） |
| **M3** | IF 作 MCP 客户端读 OpenFlow 数据；旅程/RFM 模型上线 | 旅程热力图使用真实 CDP 事件数据 |

---

## 7. 风险与规避（调研发现的坑）

1. **命名冲突**：MFlow 已有 `1-2 Insight/` 目录承载 SEO 情报资产。→ IF 报告落独立子目录，定位是"数据源插件 + 外部情报"，不复用不覆盖其报告分类。
2. **MFlow 无 API Token 机制**（密码换 session）。→ IF 保险库存 session 凭据并处理过期重登；长期方案建议 MFlow 侧增设 API token（另提需求，不在本项目改）。
3. **MFlow 偏好 CLI > MCP**。→ 对 MFlow 一律走"插件 + HTTP"，不给 MFlow 强加 MCP；MCP 只用于 OpenFlow 与外部生态。
4. **OpenFlow ApiPolicy**：`api/*.php` 新端点默认 public。→ 只用插件路由（默认 admin 档），绝不新增公共端点。
5. **发布铁律**：两个系统都有"人工授权后才对外"的文化。→ IF 动作只到"草稿/待批准"，外发动作在 OpenFlow 侧经审批门（moderate/high 风险停批）。
6. **路径与端口**：MFlow 路径含空格、console 绑定 8088/自定义端口。→ 全部走环境变量（`MFLOW_BASE_URL`、`LOVART_LOCAL_DEV_ROOT`），位置运行时推导。
