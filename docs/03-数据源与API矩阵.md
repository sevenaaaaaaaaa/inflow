# Insight Flow 数据源与 API 矩阵

> 调研执行日期：**2026-09-16**，所有定价/配额均当日自官方定价页与开发者文档核验。第三方转述数字已标注。
> 用途：Source 插件选型的依据文档。**每季度复审一次**——本行业 24 个月内已退役两大门户级 API（Bing、Google CSE）。

---

## 0. 三条结构性结论（先读这个）

1. **搜索 API 不可绑定单一供应商**。Bing Search API 已于 2025-08-11 全线退役；Google Custom Search JSON API 已停收新客户、2027-01-01 停服。任何搜索能力必须走抽象层 + ≥2 供应商。
2. **SEO 情报的成本结构已反转**。Semrush API 门槛 $549/月（且 2025-11 被 Adobe 以 $1.9B 收购、2026-04 交割，条款存在整合不确定性）；而 DataForSEO 纯按量（Google SERP 最低 $0.6/千次、$50 起充）、Ahrefs API 已随 $129/月 Lite 套餐开放。**按量制供应商应为主引擎，成品库为增值层**。
3. **免费舆情底座已经成立**：GDELT（全球新闻，免费无 Key）+ Reddit（$0.24/千次商用或免费非商用）+ YouTube（免费配额）+ 知乎数据开放平台（免费额度高，且原生支持 REST/MCP/Skills/CLI）+ iTunes Search API（免费）+ Product Hunt（免费 GraphQL）——足以支撑 MVP 全部舆情场景，商用舆情套件（$10k+/年）只留给企业档。

---

## 1. 搜索 SERP API

| 服务 | 认证 | 免费额度 | 起步价 | 速率 | 覆盖 | MCP | 结论 |
|---|---|---|---|---|---|---|---|
| **Serper.dev** | `X-API-KEY` | 2,500 次（一次性，无卡） | $50 充值=50k 次（$1/千次） | 50→300 QPS | Google 全垂直（organic/news/places/shopping，含 PAA、featured snippet） | 社区 | **MVP/生产主力**，性价比最高；credits 6 个月有效 |
| **Brave Search API** | `X-Subscription-Token` | $5 额度/月（≈1k 次，需绑卡） | 按量 $5/千次 | 50 QPS | 自家独立索引 Web/News/Images + LLM Context | 官方 | **生产级第二源**，防 Google 单点 |
| **Tavily** | Bearer | 1,000 credits/月（无卡） | PAYG ≈$8/千次 | 分档 | AI-ready 搜索+Extract+Crawl+Map | 官方 | Agent 深度研究链路首选 |
| **Exa** | API Key | $20 注册 + $10/月 | 按量 $7/千次 | 企业可调 | 语义/神经搜索，找相似产品强 | 官方 | 竞品发现（"相似站点"）场景 |
| SerpAPI | api_key | 250 次/月 | $25/月/1k 次（最贵） | 分档 | 多引擎解析最全 + Legal Shield | 社区 | 备选，贵 |
| You.com | API Key | $100 credits | 按量 $5/千次 | 分档 | Web/News/Answer | 官方托管 | 备选 |
| Google CSE | Key+cx | 100 次/天 | $5/千次，上限 1 万/天 | — | Google（受限） | 社区 | ❌ **停收新客，2027-01 停服，禁选** |
| Bing Search API | — | — | — | — | — | — | ❌ **2025-08-11 退役** |
| ddgs（DuckDuckGo 库） | 无 | 免费 | $0 + 代理成本 | 无保障 | 多后端 metasearch | 内置 | 仅开发期占位，勿进生产（合规风险自担） |

**架构要求**：统一 `SearchProvider` 接口（`search(query, vertical, config) -> SerpResult`），Insight Flow 内置适配器：`serper`、`brave`、`tavily`、`exa`、`dataforseo-serp`（5 个实现，任一故障可路由切换）。

---

## 2. SEO / 营销情报 API

| 服务 | 准入门槛 | 认证 | 计费 | 核心数据 | 结论 |
|---|---|---|---|---|---|
| **DataForSEO** | 自助注册，最低充 $50 | Basic Auth | 纯按量：SERP $0.6/$1.2/$2 每千次（Standard/Priority/Live）；Labs Live $0.012/task + $0.00012/item；含 **AI Overview 解析**与 **Ads Transparency API**（抓 Google 广告透明库） | SERP、关键词量、backlinks、竞品、Labs 市场数据 | **生产主引擎**：价格全公开、白标友好 |
| Semrush API | Advanced 套餐 $549/月起 + units 另购（约 $50/百万，第三方价） | API key | 行级扣费（organic keywords 10 units/行 live、50 历史行）；Trends 另购 | 成品化指标全（26.4B 关键词/43T 外链/142 地区库），Authority Score、流量估算、受众 | **企业档可选**（客户付费），MVP 不依赖；注意 Adobe 收购后条款风险 |
| Ahrefs API v3 | Lite $129/月起即含 API+MCP；Enterprise uncapped | Bearer | 订阅内含 Connect units（Lite 20 万/月） | backlinks 最强、Keywords、Brand Radar（AI 可见性 + 2025-12 起 Reddit/TikTok 品牌追踪） | 生产级第二来源，GEO/AI 可见性场景优先 |
| Similarweb API | 非自助，销售开通，credit 制（年约数万美元，第三方估） | API key | 合同制 | 流量估算、受众、渠道份额 | 企业档可选；**再分发需额外许可** |
| SE Ranking | 订阅自带 API（Core $129/月）；PAYG $50/250K credits | API key | 订阅+credits | 关键词/backlinks/域分析 + **AI Search 可见性端点**（ChatGPT/Perplexity/AI Overviews 品牌声量） | GEO 监测平替（比 Ahrefs 便宜） |
| Moz API | 与 Moz Pro 分离；免费层 50 rows/月 | id+secret 签名 | $20–$2,000+/月按 rows | DA/PA、links | 仅免费层做轻量 DA 参考 |
| Majestic | 独立 API 套餐 $399.99/月起 | API key | units | Trust/Citation Flow | 不选（Ahrefs/DataForSEO 已覆盖） |
| SpyFu | Pro 订阅自带（约 $39+/月） | Bearer | 含订阅内 | **2007 年至今广告史**、PPC 关键词 | 广告历史回溯特色场景 |
| DataForSEO Ads Transparency | 同 DataForSEO | 同上 | 按量 | Google 广告透明库素材与投放记录 | 广告情报（CI-4）默认实现 |

---

## 3. 第一方数据 API（全免费，MVP 底座）

| 服务 | 认证 | 配额（官方） | 核心数据 | 注意 |
|---|---|---|---|---|
| **Google Search Console API** | OAuth 2.0 | Search Analytics 1,200 QPM/site；URL Inspection 600 QPM、2,000 QPD/site | 点击/曝光/CTR/排名（query/page 维度）、索引状态、Rich Results | 数据滞后约数天；TD-1 主数据源 |
| **GA4 Data API** | OAuth / SA | 200,000 tokens/property/天、40,000/时、并发 10（360 版 10 倍） | 事件/转化/留存/受众全量 | `returnPropertyQuota:true` 查余额 |
| **Bing Webmaster API** | OAuth 2.0 | ~10k 请求/天/key（第三方数字） | Bing 关键词量、排名、外链 | **SOAP/POX 2026-08-31 退役，必须用 REST 版** |
| **CrUX API** | API key | 150 QPM/project，免费不可提额 | 真实用户 CWV（LCP/INP/CLS/TTFB），origin/url 级，28 天窗口 | PSI 的 field data 将移除，直接用 CrUX |
| Google Ads API | developer token（Keyword Planner 需 Basic+） | 15k ops/天（Basic） | 关键词量（区间值）、CPC、预测 | 规划服务限速严，仅企业档接 |
| Google Trends | ❌ 无官方 API | pytrends 已归档（2025-04），限流 1,400 次/4h | — | 趋势数据改用 Serper/Brave/Exa 交叉或 GDELT 时间线 |

---

## 4. 舆情 / 新闻 / 社媒

### 4.1 新闻与舆情底座

| 服务 | 成本 | 覆盖 | 结论 |
|---|---|---|---|
| **GDELT Project** | **完全免费**（REST DOC 2.0/GEO 2.0/TV API，CORS 全开；BigQuery 免费层 1TB/月） | 全球在线新闻 65 语言，15 分钟更新，情感 Tone、时间线聚合 | **MVP 舆情主源**；局限：仅近 3 个月滚动窗口（长历史走 BigQuery）、无社媒、无 SLA |
| NewsAPI.org | 免费档禁生产（仅 localhost）；Business $449/月 | 新闻元数据 | 生产档备选 |
| Brandwatch / Meltwater / Talkwalker(Lumen) / Digimind | 报价制，约 $10k–50k+/年 | 新闻+社媒+播客 | 仅企业档（客户自有账号凭据接入，SM-7） |
| Mention | 2025-07 涨价至 $599/月起 | 新闻+社媒 | 性价比差，观察 |

### 4.2 平台原生（结构化数据可得性分级）

| 渠道 | 可得性 | 成本/配额 | 结论 |
|---|---|---|---|
| **Reddit** | 官方 API | 非商用免费（OAuth 100 QPM）；商用 $0.24/千次 | **免费底座主力**（英文口碑/社区） |
| **知乎数据开放平台**（2025 底上线） | 官方，14 端点，**原生 REST/MCP/Skills/CLI** | 免费：搜索 1,000 次/天、热榜 10 次/天 | **国内最友好官方源**，免费底座主力 |
| YouTube Data API | 官方 | 默认 10,000 units/天（search.list 100 units，约 100 次搜索/天） | 免费底座（视频口碑）；提额需审计 |
| X (Twitter) | 官方 | **按量计费**：读 $0.005/帖；旧订阅 Basic $200/月(15k 读) 仅存量 | 选配（按客户预算），默认关 |
| Product Hunt | 官方 GraphQL v2 | 免费 6,250 complexity/15min | 免费底座（新品情报） |
| iTunes Search API | 官方 | 免费 ~20 calls/min + 评论 RSS | 免费底座（ASO/应用评论） |
| Google Reviews | Places API 硬限 5 条/place；全量仅第三方（Outscraper 类） | 按 SKU 计费 | 选配 |
| Trustpilot | 官方 API 但仅本商家数据 | 免费 | 仅客户自有口碑分析用 |
| G2 | 官方 API 约 $499/月起（另 $2,999/年供应商订阅） | 合同 | 企业档（竞品评论情报）；注意 Gartner 已把 Capterra 卖给 G2，评论入口集中 |
| 微博 | 官方 API + 2025 AI 开放平台（CLI/内容检索） | 商业 API 购配额 | 国内选配；爬虫诉讼风险高，禁爬 |
| 抖音/小红书/微信公众号 | **无公开内容检索 API** | 第三方数据商：新榜/清博/千瓜/蝉妈妈（报价制） | **合规红线：不做爬虫**；企业档经数据商接入 |

---

## 5. 网页抓取

| 服务 | 性质 | 定价 | 核心能力 | MCP | 结论 |
|---|---|---|---|---|---|
| **Firecrawl** | 开源(AGPL-3.0)+云 | 免费 1,000 credits/月；$16/月(5k)→$83(100k)→$333(500k) | JS 渲染、crawl/map/search、**LLM 结构化抽取**、监控 | 官方 | **CI-2 网站变更监控默认实现**；可自托管规避 AGPL 争议 |
| **Crawl4AI** | 纯开源 Apache-2.0 | $0（自托管） | Playwright 渲染、CSS/XPath 无 LLM 抽取、断点续爬 | Docker 内置 | 自托管批量抓取（成本敏感场景） |
| Playwright 自托管 | 开源 Apache-2.0 | $0+基建 | 全栈自动化、登录态 | 官方（37k★） | 需登录态/强反爬目标 |
| trafilatura | 开源 Apache-2.0 | $0 | 工业级正文提取 | 无 | 静态页正文提取兜底（Postlight 已停维护 2 年，勿用） |
| Jina Reader | 开源+云 | 无 Key 20 RPM；Key 500 RPM | URL→Markdown、sitemap | 官方托管 | 轻量提取备选 |
| ScrapingBee / Browserless | 商业云 | $19/月起 / $25/月起 | 代理、验证码、并发浏览器 | 官方 | 企业档/反爬困难目标 |

---

## 6. 档位化组合（采购决策）

| 档位 | 组合 | 月成本估算 |
|---|---|---|
| **L1 免费底座（MVP 默认）** | GSC + GA4 + Bing WT + CrUX（第一方）｜Serper 免费 2,500 次 + GDELT + Reddit(非商用) + 知乎 + YouTube + Product Hunt + iTunes（舆情）｜Firecrawl 1,000 credits（抓取）｜DataForSEO $50 起充用一季 | **$0–20** |
| **L2 生产级（Growth/Scale 档）** | Serper Standard 按需 + Brave 第二源 + Tavily（Agent 链路）｜DataForSEO 主力 + Ahrefs Lite（$129，backlinks/Brand Radar）｜Firecrawl $83（10 万 credits）｜Reddit 商用 | **$250–600** |
| **L3 企业级（Enterprise）** | L2 + Semrush API 或 Similarweb API（客户合同）+ G2 API + Brandwatch/Meltwater（客户自有凭据）+ 国内数据商（新榜/清博，客户付费）+ Browserless/私有化 Firecrawl | 按合同，工具成本由客户承担为主 |

---

## 7. 配额与成本治理（工程要求）

1. 每个 source 实例强制配额账本：`{calls_today, cost_month_usd, circuit_breaker: {daily_calls, monthly_usd}}`，超限自动熔断 + `quota.warning` webhook；
2. LLM 分级调用：打标/分类用低价模型（DeepSeek 级），综合推理/报告用强模型，均走统一网关记账；
3. 缓存：SERP 结果 24h 缓存（同 query+location）；网页快照按 monitor 频率去重；LLM embedding 复用；
4. 所有第三方数据入库留存 `raw_records`（证据可追溯），但**对外报告只输出结论与统计**，规避再分发条款风险。

---

## 附录：关键信息来源（2026-09-16 核验）

- Serper https://serper.dev ｜ Brave https://brave.com/search/api ｜ Tavily https://www.tavily.com/pricing ｜ Exa https://exa.ai/pricing ｜ SerpAPI https://serpapi.com/pricing ｜ You.com https://you.com/docs
- Bing 退役公告 https://learn.microsoft.com/en-us/lifecycle/announcements/bing-search-api-retirement ｜ Google CSE 停服 https://developers.google.com/custom-search/v1/overview（更新 2026-02-18）
- DataForSEO https://dataforseo.com/pricing ｜ Semrush API https://developer.semrush.com/api/v4/ ｜ Adobe 收购 Semrush https://news.adobe.com/news/2025/11/adobe-to-acquire-semrush ｜ Ahrefs https://ahrefs.com/pricing ｜ SE Ranking https://seranking.com/api.html ｜ Moz https://moz.com/products/api/pricing
- GSC 配额 https://developers.google.com/webmaster-tools/limits ｜ GA4 配额 https://developers.google.com/analytics/devguides/reporting/data/v1/quotas ｜ CrUX https://developer.chrome.com/docs/crux/api/ ｜ Bing WT REST https://learn.microsoft.com/en-us/bingwebmaster/
- GDELT https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/ ｜ Reddit Data API Terms https://redditinc.com/policies/data-api-terms ｜ X 定价 https://docs.x.com/x-api/getting-started/pricing ｜ 知乎数据开放平台 https://open.zhihu.com（2025 底上线） ｜ YouTube 配额 https://developers.google.com/youtube/v3/determine_quota_cost
- Firecrawl https://www.firecrawl.dev/pricing（2026-09-04 新价）｜ Crawl4AI https://github.com/unclecode/crawl4ai ｜ trafilatura https://github.com/adbar/trafilatura
