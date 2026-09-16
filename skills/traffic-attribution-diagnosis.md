---
name: traffic-attribution-diagnosis
description: 流量归因诊断：输入时间窗，定位流量波动根因（渠道/关键词/技术三向）
triggers: [流量, traffic, 归因, 诊断, 下降]
inputs: {window_days: int = 14}
tools: [query_insights, get_insight_detail, list_reports, get_feedback_stats]
---

# 执行步骤

1. `query_insights` 拉取 traffic_anomaly / conversion_low / keyword_opportunity 类洞察
2. 按 AARRR 阶段分组：Acquisition（GSC 渠道）→ Activation（GA4 会话）→ Revenue（转化）
3. 对每个异常洞察调用 `get_insight_detail`，核对证据里的环比数据（骤变幅度、Z-score）
4. 三向归因：渠道变化（referral/social）· 关键词排名（GSC）· 技术健康（CrUX CWV）
5. 输出结论时引用具体 insight id 与 evidence 类型

# 判定优先级

1. 技术问题优先排除（CWV 劣化会导致全渠道下降）
2. 其次渠道单点（单一来源骤降 = 渠道风险）
3. 最后内容/排名（关键词波动）
