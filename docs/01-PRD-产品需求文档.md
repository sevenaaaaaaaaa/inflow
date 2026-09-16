# Insight Flow 产品需求文档（PRD）

| 项 | 内容 |
|---|---|
| 产品名 | Insight Flow（工程代号 `insflow`） |
| 版本 | PRD v1.0 |
| 日期 | 2026-09-16 |
| 作者 | 增长产品组 |
| 关联系统 | OpenFlow（本机 `~/OpenFlowDev`）、MFlow（本机 `~/MFlow Dev`） |
| 配套文档 | 02-技术架构 · 03-数据源矩阵 · 04-集成方案 · 05-竞品调研 · 06-路线图 |

---

## 1. 背景与机会

### 1.1 问题

任何一家公司在增长的任意阶段，都要反复回答同一组问题：

- **冷启动**：市场里谁在赢？用户真正的需求语言是什么？从哪个缝隙切入？
- **增长获客**：流量掉在哪？竞品抢走了什么词、投了什么广告？下一个渠道机会在哪？
- **全生命周期运营**：用户从看到→买→留→推荐，旅程断在哪一环？口碑在说什么？
- **商业化**：定价和包装是否匹配价值？LTV:CAC 是否健康？复购/增购的杠杆在哪？

今天回答这些问题需要拼装 5–10 个工具（Similarweb 看流量、Semrush 看词、Brandwatch 看舆情、Crayon 看竞品、再看 GA4/GSC……），且**看得到洞察、落不了地**——洞察停留在报表里，执行仍靠人肉搬运。

### 1.2 机会（来自 2026-09 市场调研，详见文档 05）

1. **海外价格带空档**：$14–400/月的监控/SEO 工具（Visualping、Semrush）与 $15k–50k/年的企业 CI 套件（Crayon、Klue、Kompyte）之间，缺少"中端价格 + 全渠道 + 面向增长团队"的产品。CI 三强的重心是销售 Battlecard，几乎不做增长分析。
2. **国内空白**：5118/站长之家偏 SEO 工具，新榜/清博/蚁坊全是报价制政企/品牌客户生意，**没有公开定价、自助订阅的增长情报 SaaS**。
3. **供给侧正在松动**：DataForSEO 按量计费（SERP $0.6/千次）、Ahrefs API 门槛降至 $129/月、GDELT/Reddit/知乎数据开放平台构成免费数据底座——聚合型情报产品的原料成本历史最低。
4. **独特优势**：我们自有 OpenFlow（增长执行操作系统，CDP+自动化+Agent）与 MFlow（内容营销流水线），别人做"情报到报表"为止，我们可以做**情报到执行到验证**的完整闭环——这是 Crayon/Klue/Similarweb 都不具备的。

### 1.3 一句话定位

> **Insight Flow 是增长团队的"情报中枢 + 策略大脑 + 执行开关"**：持续采集市场/竞品/流量/舆情数据，用洞察模型与 Agent 把数据变成带证据的结论和推荐动作，一键路由到 OpenFlow/MFlow 执行，并用回流数据验证每条洞察的有效性。

---

## 2. 目标用户与场景

### 2.1 用户角色

| 角色 | 描述 | 高频场景 | 付费能力 |
|---|---|---|---|
| 创始人/一人公司 | 自己做增长，时间碎片化 | 每周看一份"该做什么"的决策简报 | 低（自助 $49–199/月） |
| 增长负责人/增长团队 | 中小团队增长 Owner | 竞品异动告警、渠道诊断、实验选题 | 中（$199–999/月） |
| 代理公司/咨询顾问 | 服务多家客户 | 批量客户诊断报告、数据成熟度评估 | 中高（多 Workspace + 白标） |
| 企业增长/市场团队 | 自有 OpenFlow/MFlow 部署 | 私有化部署、与内部 CDP/内容管线打通 | 高（私有化 + 年费） |

### 2.2 核心用户故事（节选）

- 作为创始人，我每周一早上收到一页纸简报：竞品这周做了什么、我的流量为什么变了、建议我做的 3 件事（可一键派发执行）。
- 作为增长负责人，当某核心关键词排名跌幅 >3 位或竞品上线新定价页时，我要在 1 小时内收到告警，且告警里带"证据 + 建议动作"。
- 作为顾问，我输入客户域名 + 3 个竞品，10 分钟内得到一份含数据成熟度评分、阶段判定和 90 天增长路线的 bracing 报告。
- 作为 OpenFlow 用户，我的 Agent 在自主执行 Loop 时能直接查询 Insight Flow："最近 7 天竞品有什么值得跟进的动作？"并把结果写回 CDP。
- 作为 MFlow 用户，我的内容流水线每天自动接收 Insight Flow 推送的选题 brief（含搜索需求、竞品缺口、舆情钩子），生成的内容发布后，效果数据自动回流验证该选题假设。

---

## 3. 核心概念模型

### 3.1 增长阶段（Stage）——产品的组织主轴

Insight Flow 不按功能组织，按**增长阶段**组织。每个 Workspace 入场先做数据成熟度评估，系统判定当前阶段，界面与洞察推送围绕阶段展开：

| 阶段 | 定义 | 核心问题 | 主用模块 |
|---|---|---|---|
| S0 冷启动 | 产品未上线/上线<6 月，PMF 未验证 | 市场谁在赢？切入点在哪？ | 竞品情报、舆情需求挖掘（JTBD）、数据成熟度 |
| S1 增长获客 | PMF 已验证，追求规模化获客 | 流量/线索为什么波动？下一个渠道在哪？ | 流量诊断、竞品 SEO/广告情报、关键词机会 |
| S2 全生命周期运营 | 有稳定用户盘，追求留存与价值深挖 | 旅程断在哪？谁要流失？谁可增购？ | 客户旅程、舆情口碑、RFM/分群（联动 OpenFlow CDP） |
| S3 商业化优化 | 收入模型待优化 | 定价/包装/复购杠杆在哪？ | 转化漏斗、定价监控、LTV:CAC 健康度 |

阶段可并存（大型客户多阶段并行），由成熟度评分 + 用户确认共同决定。

### 3.2 数据→行动闭环（产品的核心循环）

```
① 采集 Collect   ──►  ② 归一 Normalize  ──►  ③ 建模 Model     ──►  ④ 洞察 Insight
(40+ 数据源插件)      (统一指标/实体)        (Builtin+Custom)     (证据+置信度+推荐动作)
                                                                      │
⑥ 回流 Feedback  ◄──  ⑤ 执行 Act      ◄──────────────────────────────┘
(效果数据验证假设)      (OpenFlow / MFlow / Webhook)
      │
      └──► 模型权重与 Playbook 迭代（哪类洞察真的带来增长）
```

### 3.3 洞察对象（Insight）——一等公民

一切产出的最小单元，全系统统一 schema（详见架构文档）：

```json
{
  "id": "ins_20260916_001",
  "type": "competitor_pricing_change",
  "title": "竞品 A 于 9/14 将 Pro 档从 $49 提至 $59",
  "summary": "竞品 A 定价页 Pro 档上涨 20%，同时上线年付 8 折；其 SEO 流量近 30 天 +12%",
  "severity": "warning",             // info | watch | warning | critical
  "confidence": 0.92,
  "evidence": [                       // 每条结论必须带证据
    {"source": "web_diff", "detail": "pricing 页 diff 见快照", "captured_at": "..."},
    {"source": "dataforseo", "metric": "organic_traffic", "delta": "+12% 30d"}
  ],
  "models": ["pricing_monitor", "competitor_momentum"],
  "recommended_actions": [            // 洞察必须可执行
    {"action": "mflow.create_content", "label": "生成对比页内容", "params": {...}},
    {"action": "openflow.automation", "label": "触发销售话术更新", "params": {...}}
  ],
  "stage_tags": ["S1", "S3"],
  "status": "new"                     // new → acknowledged → actioned → verified | dismissed
}
```

**验收铁律**：任何模型/Agent 产出的"洞察"若没有 `evidence` 或没有 `recommended_actions`，不得进入推送与报告。

---

## 4. 功能需求

优先级定义：**P0** = MVP 必须；**P1** = V1 必须；**P2** = V2+ 规划。

### 4.1 模块一：数据成熟度分析（Data Maturity）——入场钩子

**价值**：用户入场 10 分钟内获得"我在哪、先补什么"的判定，天然导出产品使用路径（先修数据 → 再看诊断 → 再上执行）。

| ID | 需求 | 优先级 |
|---|---|---|
| DM-1 | 成熟度评估问卷（约 30 题以内）：埋点/工具栈/数据治理/归因能力/自动化程度五个维度，映射到 L0–L4 五级（参考 BCG 数字营销成熟度模型与 DAMA 治理框架） | P0 |
| DM-2 | 自动核验：用户授权 GSC/GA4 后，系统自动核验问卷自报数据（如"已部署 GA4"→ 实测能否拉数；"有结构化数据"→ URL Inspection + CrUX 实测），生成"自报 vs 实测"差异报告 | P1 |
| DM-3 | 产出《数据成熟度报告》：雷达图 + 五级定位 + Top5 补课清单（每项关联具体模块功能） | P0 |
| DM-4 | 阶段判定引擎：综合成熟度、流量规模、转化数据、用户确认，输出 S0–S3 阶段标签，驱动全站信息架构 | P0 |
| DM-5 | 成熟度追踪：每季度自动重评，展示升级轨迹 | P2 |

### 4.2 模块二：流量诊断（Traffic Diagnosis）

**价值**：把"我自己的数据 + 第三方估算 + 竞品对标"放进一张诊断台，用 AARRR 骨架定位掉点。

| ID | 需求 | 优先级 |
|---|---|---|
| TD-1 | 第一方接入向导：GSC（Search Analytics 全维度）、GA4 Data API（事件/转化/留存）、Bing Webmaster（REST 版）、CrUX（CWV 性能）；OAuth 流程 + 配额感知（GSC 1,200 QPM/site，GA4 200k tokens/天/property） | P0 |
| TD-2 | **流量诊断台**：按 AARRR 五段映射渠道指标，自动标注异常（z-score/环比阈值可配），每处异常生成 Insight（含证据链） | P0 |
| TD-3 | 渠道分解：Organic / Paid / Referral / Social / Direct / AI-Search（GSC 中 AI Overview 引流单独标注）分趋势对比 | P1 |
| TD-4 | 技术健康扫描：CrUX CWV + 站点可达性 + sitemap/robots 检查 + 结构化数据检测（URL Inspection API），输出可执行修复清单 | P1 |
| TD-5 | 第三方对标：DataForSEO/Semrush/Ahrefs 拉竞品流量估算，与自有真实数据同图对比（明确标注"估算"） | P1 |
| TD-6 | 周/月自动诊断报告：Markdown 报告落文件系统 + 推送摘要到飞书/Webhook | P1 |

### 4.3 模块三：竞品情报（Competitive Intelligence）

| ID | 需求 | 优先级 |
|---|---|---|
| CI-1 | 竞品档案（Competitor Profile）：域名、定位、定价档位、产品线、社媒矩阵、监测项清单 | P0 |
| CI-2 | **网站变更监控**：定价页/产品页/changelog/博客 RSS 定时抓取（Firecrawl/自托管 Playwright），视觉 diff + 语义 diff（LLM 判断"这次改版意味着什么"），生成告警 | P0 |
| CI-3 | SEO 竞争情报：关键词重叠/缺口（Keyword Gap）、排名追踪、外链新增、竞品流量趋势（DataForSEO Labs，成本可控） | P0 |
| CI-4 | 广告素材情报：Google Ads Transparency Center 抓取（DataForSEO Ads Transparency API）、Meta Ad Library（政策允许范围内）、投放力度变化曲线 | P1 |
| CI-5 | 产品动态雷达：竞品 changelog/App 更新（iTunes Search API 免费）/Product Hunt 上新/招聘页 JD 变化（团队扩张方向推断）聚合时间线 | P1 |
| CI-6 | 竞争格局地图：以关键词簇/受众重叠度自动生成竞争象限图（类似 Perceptual Map） | P2 |
| CI-7 | 周报《竞品动向》：全渠道聚合 + LLM 摘要"本周最值得注意的 3 件事" | P1 |

### 4.4 模块四：舆情收集（Sentiment & Voice of Market）

| ID | 需求 | 优先级 |
|---|---|---|
| SM-1 | 免费底座接入：GDELT（全球新闻，免费）、Reddit（官方 API）、YouTube Data API、Product Hunt、知乎数据开放平台（免费额度 + 原生 MCP）、App 评论（iTunes Search API） | P0 |
| SM-2 | 主题监控（Monitor）：品牌词/竞品词/品类词 × 渠道矩阵，定时采集 → 归一化存储 | P0 |
| SM-3 | 情感与主题分析：LLM 批量打标（情感极性/主题簇/JTBD 任务归类/紧急度），支持自定义打标 schema | P0 |
| SM-4 | 口碑漏斗：评论/帖子 → 需求语言库（JTBD）→ 内容选题建议（可直接推送 MFlow） | P1 |
| SM-5 | 舆情告警：声量突增/负面突增/大V提及，分级推送（飞书/Slack/Webhook） | P1 |
| SM-6 | 国内社媒扩展：微博开放平台（含 2025 AI 开放平台 CLI）、新榜/清博/千瓜数据商对接（企业档）；**不提供小红书/微信全量爬取**（合规红线） | P2 |
| SM-7 | 企业舆情 API 档位：Brandwatch/Meltwater 对接（客户自有账号凭据接入） | P2 |

### 4.5 模块五：客户旅程（Customer Journey）

| ID | 需求 | 优先级 |
|---|---|---|
| CJ-1 | 旅程框架内置：See-Think-Do-Care（默认）/ AIDA / 自定义五段，每阶段定义进入信号与衡量指标 | P0 |
| CJ-2 | 触点映射：自动把自有内容/广告/竞品内容映射到旅程阶段，输出"旅程覆盖热力图"（哪个阶段内容/投放空心） | P1 |
| CJ-3 | 真实旅程重建（联动 OpenFlow）：读取 OpenFlow CDP 事件流与分群，重建匿名→会员→成交的身份旅程，标注断点 | P1 |
| CJ-4 | 旅程断点 Insight：如 "Think 阶段搜索需求 Top20 中我们仅覆盖 3 篇"，直接生成内容缺口清单（→ MFlow） | P1 |
| CJ-5 | RFM/价值分层视图：订单/CRM 数据（OpenFlow leads/orders 或 CSV 导入）→ RFM 分群 + 分群策略建议 | P2 |

### 4.6 模块六：定制化洞察模型（Insight Models）

**价值**：内置模型库开箱即用；模型即插件（`model` 类型），用户/生态可自定义。

| ID | 需求 | 优先级 |
|---|---|---|
| IM-1 | **内置模型库 v1**（每个模型 = 输入指标 + 规则/统计 + 输出 Insight schema）：AARRR 漏斗诊断、LTV:CAC 健康度（阈值 LTV:CAC≥3、回收期<12 月）、竞品动量（多信号加权）、关键词机会分（搜索量×竞争力×业务相关）、留存健康度（RARRA 视角）、定价异动、NPS 趋势 | P0 |
| IM-2 | 模型 = 插件：`type: model`，manifest 声明输入指标依赖与输出 Insight 类型，`evaluate(ctx) -> insights[]`，热加载 | P0 |
| IM-3 | 自定义规则模型（无代码）：JSON DSL 定义"指标 + 条件 + 阈值 + 洞察模板"，面向增长运营自助配置 | P1 |
| IM-4 | Growth Loop 画布：把模型发现的"可循环机制"（内容循环/病毒循环/数据循环）可视化为 Loop 图，并映射到 OpenFlow 自动化模板 | P2 |
| IM-5 | 模型效果追踪：每条 Insight 关联后续动作与回流效果，按模型聚合"洞察命中率"，驱动权重调优 | P1 |

### 4.7 模块七：数据洞察 Agent（Insight Agent）

| ID | 需求 | 优先级 |
|---|---|---|
| AG-1 | 问答分析（Ask）：自然语言提问 → 工具调用（查库/API/抓取）→ 带图表与引用的回答；所有数字必须可溯源到证据 ID | P0 |
| AG-2 | 定时分析员：每个 Workspace 可配置"每日/每周分析员"任务（提示词 + 数据范围 + 输出模板），产出 Markdown 报告 | P1 |
| AG-3 | Skill 体系：Agent 能力以 Skill（Markdown+YAML，frontmatter 声明触发条件/输入输出/依赖工具）组织，内置首批 Skill：竞品动向分析、流量归因诊断、舆情主题挖掘、旅程缺口分析、增长实验设计（ICE 评分） | P0 |
| AG-4 | 对外 MCP Server：暴露 `list_insights / get_insight / run_diagnosis / ask_analyst / list_competitors / get_maturity / trigger_playbook` 等工具，供 OpenFlow AgentRuntime、Claude、Cursor 等消费 | P0 |
| AG-5 | 深度报告生成：输入域名 + 竞品清单 → 多 Agent 协作（采集/分析/写作）→ 完整增长策略报告（顾问场景的核心交付物） | P1 |
| AG-6 | 成本治理：LLM 网关 OpenAI 兼容（DeepSeek/OpenAI/自建），按 Workspace 预算记账与熔断（对齐 OpenFlow AiBudget 思路） | P0 |

### 4.8 模块八：执行落地与集成（Action & Integration）——产品差异化核心

| ID | 需求 | 优先级 |
|---|---|---|
| AC-1 | **OpenFlow 集成（官方插件）**：双向——(a) 出站：监听 `cdp_event_received / payment_success / crm_deal_won / cdp_segment_enter` 等钩子推送事件进 Insight Flow；(b) 入站：`register_api_route` 提供 `/api/plugin/insight-flow/insights` 回读、`register_mcp_tool` 注册 `insight_flow_query` 供 AgentRuntime 消费、设置页配置凭据、`register_schedule` 定时同步 | P0 |
| AC-2 | **MFlow 集成（官方 source 插件）**：`plugins/insight-flow/`（type: source），`collect(cfg)` 拉取每日洞察选题包写入 `$LOVART_LOCAL_DEV_ROOT/Output/Data Ingestion/`；反向通过 MFlow 控制台 `POST /api/loop/create` 触发内容生产、`GET /api/loop/detail` 轮询取稿 | P0 |
| AC-3 | 动作路由器（Action Router）：Insight.recommended_actions → 目标系统调用；内置 action 适配器：`mflow.*`、`openflow.*`、`webhook.generic`、`feishu.notify`；动作执行记录可追溯 | P0 |
| AC-4 | 出站 Webhook：`insight.created / monitor.alert / report.ready / action.executed / feedback.received` 事件，HMAC-SHA256 签名 + 重试退避，兼容 n8n/Zapier/Make/Dify | P0 |
| AC-5 | 入站采集口：`POST /api/v1/ingest`（HMAC 鉴权），接收 OpenFlow InboundReceiver、外部系统事件，归一化入库 | P1 |
| AC-6 | 效果回流验证：动作执行后自动挂"验证窗口"（默认 14 天），到期拉取 GSC/GA4/排名数据对比基线，输出"洞察验证报告"（该建议是否真的有效） | P1 |
| AC-7 | OpenAPI 3.1 规范发布 + 官方 API Key 管理（对齐 OpenFlow ApiKeyAuth 多 Key 体系：key+secret、权限、到期、IP 白名单） | P1 |

### 4.9 平台需求

| ID | 需求 | 优先级 |
|---|---|---|
| PL-1 | 多 Workspace（租户）隔离：数据、凭据、模型、报告全隔离；代理场景多客户切换 | P1 |
| PL-2 | 凭据保险库：所有第三方 API Key 加密落盘（AES-GCM，主密钥环境变量），界面只显尾四位 | P0 |
| PL-3 | 调度中心：采集/报告/验证任务的统一调度（cron 表达式 + 依赖编排 + 失败重试 + 熔断） | P0 |
| PL-4 | 报告中心：所有报告 Markdown 文件存储 + 分类目录（对齐 MFlow `1-2 Insight` 习惯），支持在线渲染与导出 PDF | P1 |
| PL-5 | 审计日志：登录、动作执行、凭据变更、插件安装全量审计 | P1 |
| PL-6 | RBAC：Owner / Admin / Analyst / Viewer 四角色 | P2 |

---

## 5. 非功能需求

| 类别 | 要求 |
|---|---|
| 性能 | 诊断台首屏 < 2s（缓存聚合）；Ask 问答 P95 < 30s（含工具调用）；单 Workspace 支撑 50 竞品 × 200 关键词监控 |
| 成本 | MVP 阶段月度数据源 + LLM 成本预算 <$100（详见数据源矩阵的分层组合）；所有外部调用有配额账本与熔断 |
| 可用性 | 自托管单机可跑（SQLite WAL + 文件存储）；无消息队列依赖；Docker Compose 一键部署；数据可随时打包导出（防锁定） |
| 安全 | 密钥加密存储；API 全量审计；出站请求 SSRF 防护（对齐 OpenFlow `conn_request`）；插件权限声明 + 静态校验（对齐 MFlow plugin_check） |
| 合规 | 遵守 robots.txt 与目标站 ToS；不碰国内平台爬虫红线；第三方数据再分发前核对条款（Similarweb/Ahrefs/Semrush 均限制白标）；GDPR/个保法：旅程模块处理 CDP 数据时支持字段最小化与匿名化 |
| 可扩展 | Source/Model/Action/Template 四类插件 + Skill 双扩展体系；MCP + REST + Webhook 三种对外协议 |
| 兼容 | LLM 网关 OpenAI 兼容协议（DeepSeek/OpenAI/本地 Ollama 均可）；飞书/Slack 通知双支持 |

---

## 6. 信息架构（Web 控制台）

```
总览 Dashboard（今日必读 3 条洞察 · 阶段徽章 · 周简报入口）
├── 诊断 Diagnose：流量诊断台 · 技术健康 · 数据成熟度
├── 竞品 Competitors：竞品档案 · 变更动态时间线 · SEO/广告情报 · 竞争格局图
├── 舆情 Listening：主题监控 · 情感趋势 · 需求语言库(JTBD) · 告警规则
├── 旅程 Journey：旅程框架 · 覆盖热力图 · 断点清单 · RFM 分群
├── 洞察 Insights：洞察流(全部) · 模型库 · 模型效果 · 自定义规则
├── 行动 Actions：动作路由 · OpenFlow/MFlow 连接状态 · 执行历史 · 验证报告
├── 报告 Reports：周报/月报/诊断报告/深度报告（Markdown 中心）
├── 数据源 Sources：插件市场 · 已装插件 · 凭据保险库 · 配额账本
└── 设置 Settings：Workspace · 团队 · API Keys · Webhook · MCP · 审计
```

---

## 7. 商业模式建议

| 档位 | 定价（建议） | 能力边界 | 对应数据源组合 |
|---|---|---|---|
| Free / 自托管开源 | $0（开源核心，核心自托管） | 单 Workspace、免费数据源底座（GSC/GA4/GDELT/Reddit/知乎/CrUX）、OpenFlow/MFlow 集成 | 层级1 免费底座 |
| Growth | $99–199/月 | + 竞品监控 3 个、DataForSEO 额度、Agent 问答、周报 | 层级2 |
| Scale | $499–999/月 | + 竞品 20 个、广告情报、多渠道告警、API/MCP、白标报告 | 层级2–3 |
| Enterprise / 私有化 | 年费报价制 | 私有化部署、Similarweb/Brandwatch 等企业源接入（客户自有凭据）、SSO、审计 | 层级3 |

设计原则：**开源核心 + 托管增值**（open-core）。开源核心保证生态与插件增长（与 OpenFlow 的开源定位一致）；托管版收费点是"替用户跑采集 + 付第三方 API 成本 + Agent 算力"。DataForSEO 按量计费模式使每档位边际成本可精确核算，避免 Semrush 式"套餐+units"黑盒。

---

## 8. 成功指标（North Star + 护栏）

- **北极星**：每周"被采纳的动作数"（Insight → acknowledged → actioned 的条数）——衡量洞察到执行的真实转化。
- 护栏指标：洞察→执行率 > 30%；验证报告回收率 > 80%；告警误报率 < 10%；单 Workspace 月数据成本 < 售价 25%；M0 起 90 天内 OpenFlow/MFlow 双集成跑通闭环。

---

## 9. 风险与对策

| 风险 | 等级 | 对策 |
|---|---|---|
| 上游 API 政策突变（Bing/Google CSE 前车之鉴；Adobe 收购 Semrush 后条款不确定） | 高 | 全部数据源走 Source 插件抽象层；任一品类保持 ≥2 供应商；关键调研结论写入数据源矩阵并每季复审 |
| 第三方数据再分发合规 | 高 | 托管版向客户展示分析结论而非原始数据行；企业源要求客户自有凭据；白标输出走合同审查 |
| LLM 成本失控 | 中 | 预算账本 + 按模型分级（便宜模型做打标、强模型做综合）+ 缓存复用 |
| MFlow `1-2 Insight/` 目录命名冲突（该目录已承载 SEO 情报资产） | 低 | Insight Flow 对 MFlow 的输出定位为"数据源插件 + 独立报告目录"，复用而非覆盖其报告中心分类；集成方案已明确规避 |
| 竞品（Semrush EyeOn 等）补齐"洞察到执行" | 中 | 加深 OpenFlow/MFlow 生态绑定（这是独占资产）；开源插件生态建立迁移成本 |
| 国内数据商（新榜/清博）报价制拉高成本 | 中 | 国内社媒数据仅企业档提供且由客户付费；免费档不含 |

---

## 10. 明确不做（Non-goals）

1. 不做全平台爬虫矩阵（小红书/微信公众号全量抓取等灰色地带）——用官方平台或数据商。
2. 不做通用 BI/数仓——数据湖只保留洞察所需指标，重度分析导出到用户自有设施。
3. 不做投放执行（代操作广告账户）——投放动作路由到 OpenFlow 连接器或用户自有工具。
4. 不做面向销售团队的 Battlecard 产品（Crayon/Klue 主场）——聚焦增长团队。
