<div align="center">

# Insight Flow（inFlow）

**大增长情报 OS：全域数据监测与洞察 —— 采集 → 洞察 → 策略 → 执行 → 回流**

![Language](https://img.shields.io/badge/Language-Python%203.12%2B-blue)
![Version](https://img.shields.io/badge/Version-1.0.0-green)
![Tests](https://img.shields.io/badge/%E6%B5%8B%E8%AF%95-800%2B-brightgreen)
![License](https://img.shields.io/badge/License-MIT-blue)

[官网](https://nownexts.com) · [使用指南](docs/USAGE-GUIDE.md) · [相关仓库](https://github.com/sevenaaaaaaaaa)

</div>

## 这是什么

「竞品是不是又降价了？」「我的品牌词最近被谁在提？」「官网哪一步在漏客户？」——这些问题的答案散落在十几个工具和数据源里，普通团队既没人力天天盯，也没精力把数据拼到一起。inFlow 把这些数据源接到一个地方，自动盯、自动算、自动告诉你该干什么：每条洞察都带证据、置信度和推荐动作，不是一句「建议关注」。

它不是又一个报表看板。看板告诉你「发生了什么」，inFlow 回答「所以呢」：洞察强制携带推荐动作，动作直接落地到自动化任务、内容任务或任意 Webhook，14 天验证窗口回查效果，模型按命中率自我调优。监测对象也不限于网站——品牌词、话题、竞品、行业事件、账号、平台热榜都能盯，不要求你有域名或自有站点。

v1.0.0 已完成商业交付（首个付费客户签约），已验证的能力规模：12 个数据源插件、8 个内置模型 + 自定义 DSL、6 个动作适配器、MCP Server 10 工具、REST API 33 条路径、800+ 自动化测试。

一图看懂：

```
┌────────────── 上游数据源（插件化，永不绑定）──────────────┐
│  搜索 SERP   SEO 情报      第一方数据    舆情/新闻   社媒/抓取 │
│  Serper      DataForSEO    GSC / GA4    GDELT      Reddit   │
│  Brave       Semrush       CrUX         NewsAPI    Firecrawl│
│  Tavily/Exa  Similarweb    Google Ads   知乎(官方)  Crawl4AI │
└────────────────────────┬─────────────────────────────-──┘
                         ▼
┌────────────── Insight Flow 核心 ────────────────────────┐
│  采集管线 Collector → 归一化数据湖(SQLite+文件)            │
│  → 洞察引擎(8 内置模型 + 自定义 DSL)                      │
│  → 数据洞察 Agent(6 Skill)→ Insight(证据+置信度+推荐动作)  │
│  → 数据成熟度评估 → 增长阶段判定 → 策略剧本 Playbook        │
│  对外：REST API(33 路径) · MCP(10 工具) · Webhook(HMAC)   │
└────────────────────────┬─────────────────────────────-──┘
                         ▼
┌────────────── 执行落地 + 效果回流 ────────────────────────┐
│  OpenFlow(执行 OS)· MFlow(内容管线)· 飞书/Slack · n8n/Dify │
│  → 14 天验证窗口回查 → 验证报告反馈模型效果（闭环）          │
└────────────────────────────────────────────────────────┘
```

## 核心能力

- **全域采集，数据源永不绑定** —— 搜索 / SEO / 新闻舆情 / 社媒 / 网页抓取 / 第一方数据走统一的 Source 插件接口（v1.0 实装 12 个，选型矩阵 40+ 源），上游关停换插件即可，教训来自 Bing 退役与 Google CSE 停服。
- **流量诊断** —— 从 GSC / GA4 找出高曝光低点击的捡漏词、漏斗里流失最狠的一步，给出先修哪个的排序。
- **舆情监测** —— 品牌词/话题在全网新闻的提及量时序与情绪分布（中英双语情感词典），异常热度与负面自动预警。
- **竞品情报** —— 定时抓竞品定价页 / Changelog，价格异动和大改版经语义 diff 自动生成洞察，不用人工刷竞品官网。
- **客户旅程** —— GA4 漏斗事件自动计算相邻步断点，流失预警直接触发动作，旅程断点不靠人翻报表发现。
- **定制洞察模型 + 数据成熟度评估** —— 运营写一段 JSON 规则（latest / any / trend 三窗口）就是一个新模型，静态校验兜底；先判断你的数据基础在哪一级，再决定补什么。
- **数据洞察 Agent** —— 6 个内置 Skill（竞品异动 / 流量归因 / 市场之声 / 旅程断点 / 增长实验 / 周报撰写），问一句给结论、带证据。
- **洞察必须可执行，14 天自动验证** —— 每条 Insight 强制携带推荐动作，经 6 个动作适配器落地（OpenFlow / MFlow / 飞书 / Slack / 通用 Webhook），验证窗口到期出验证报告，反馈模型效果。

五条设计原则，决定了它和传统 BI 工具的差别：

1. **洞察必须可执行**：没有推荐动作的洞察不允许出引擎。
2. **数据源永不绑定**：所有上游走 Source 插件接口，Bing 退役、Google CSE 停服的教训写进了架构。
3. **插件 + Skill 双扩展**：数据源/模型/执行通道用插件（manifest.json + entry.py，与 MFlow 插件规范同构），Agent 能力用 Skill（跨工具可复用）。
4. **文件优先、轻依赖**：SQLite + Markdown/JSON 文件存储，无消息队列、无重中间件，个人可自托管。
5. **合规红线**：不爬平台私域全量，遵守 robots.txt 与 API 再分发条款。

## 快速上手

```bash
# 依赖：Python 3.12+
git clone https://github.com/sevenaaaaaaaaa/inflow.git && cd inflow
pip install -e ".[dev]"

insflow init                            # 初始化工作区
insflow workspace create "我的工作区"     # 建第一个工作区
insflow serve --reload                  # 启动服务（默认端口 8400）
```

打开 Web 控制台 `http://localhost:8400/console`：仪表盘、接入向导、洞察流、监控、报告中心、插件市场、套餐用量都在里面（零构建链，SSR 直读数据）。第一件事是按接入向导接一个数据源（比如免费起步的 GDELT 品牌提及监控），然后就能在洞察流里看到第一批带证据的洞察。

命令行党：`insflow run diagnosis`（流量诊断）· `insflow agent ask "竞品最近有什么动作"` · `insflow report weekly` · `insflow export`（JSON/CSV/MD 导出，防锁定）。

扩展它只需要两步：`insflow plugin new source my-blog` 生成插件骨架，`insflow plugin check plugins/sources/my-blog` 过静态校验（明文密钥扫描等）即可进调度器。Agent 能力用 Skill（Markdown + YAML，跨工具复用），Prompt 版本与 golden set 回归见 [prompts/](prompts/)。接口契约由 OpenAPI 3.1 快照锁定（CI 阻断破坏性变更），事件目录自动生成勿手改（[docs/14-事件目录](docs/14-事件目录.md)）。

更多接入示例见 [examples/](examples/README.md)（curl / Python / TypeScript / n8n / Dify / MCP）。

接好数据源后，四类无人值守监控立即可用（cron 驱动、任务持久化、服务重启自动恢复）：

| 监控类型 | 数据源 | 产出 |
|---|---|---|
| 网站变更 | Firecrawl 抓定价页 / Changelog | 价格异动 + 内容大改语义 diff 洞察 |
| 关键词机会 | GSC query 维度 | 高曝光低 CTR 捡漏清单 |
| 品牌提及 | GDELT 全球新闻 | 讨论量时序 + 舆情信号 |
| 客户旅程 | GA4 漏斗事件 | 相邻步断点 + 流失预警 |

## 与 OpenFlow 的关系

inFlow 属于进阶层：单独用它就是一套完整的「监测 → 洞察 → 报告」交付，不依赖任何其他系统；要动手执行时再接执行侧。

- 纯洞察交付 → 装它一个就够：控制台看数、周报定时送达、API/MCP 喂给你的任何工作流。
- 已在用 OpenFlow → 洞察经零插件通道落 OpenFlow CDP，或走官方插件（7 个业务钩子出站、洞察回读 API、设置页），被 GrowthBrain / 画布 / Agent 消费。
- 已在用 MFlow → `mflow.create_content` 一键产稿（只创建任务，绝不代用户发布），发布结果经 webhook 回流进验证状态机。

互通只走公开 API（HMAC / API Key），不共享数据库；拔掉任何一侧，inFlow 照常运转。

## 使用指南

完整使用指南见 **[docs/USAGE-GUIDE.md](docs/USAGE-GUIDE.md)**。

深入文档：[产品需求文档](docs/01-PRD-产品需求文档.md) · [技术架构](docs/02-技术架构设计.md) · [数据源与 API 矩阵](docs/03-数据源与API矩阵.md)（40+ 源选型）· [插件开发指南](docs/15-插件开发指南.md) · [运维手册](docs/08-运维手册.md) · [战略定位](docs/09-战略定位-全域洞察与超级个体.md) · [路线图](docs/06-路线图与里程碑.md)。

## 当前边界

- 合规红线：不爬小红书 / 微信公众号全量（走官方开放平台或数据商），遵守 robots.txt 与各 API 再分发条款。
- v1.0 实装 12 个数据源插件；文档中的 40+ 源矩阵是选型清单，其余按需逐个实装，接入深度以插件市场实际为准。
- 企业级舆情源（如 Brandwatch）为选型调研结论，需要相应商业订阅，未含在开源仓库内。
- 广告平台直连拉取（Meta / Google Ads API）属路线图项，当前第一方数据走 GSC / GA4。
- 自助注册与套餐自助开通（超级个体自助版）在路线图 R4，当前以自托管 + 交付式部署为主。

## License

MIT（声明见 `pyproject.toml`）。
