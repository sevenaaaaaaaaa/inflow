---
name: voice-of-market-mining
description: 市场之声挖掘：从舆情洞察中提取需求信号（JTBD）与高频抱怨主题
triggers: [舆情, 市场声音, reddit, 知乎, 抱怨, 需求]
inputs: {topic: string, limit: int = 20}
tools: [query_insights, get_insight_detail]
---

# 执行步骤

1. `query_insights` 检索 voice_of_market / competitor_mention / nps 类洞察
2. `get_insight_detail` 读取证据原文（Reddit 帖子/知乎回答/新闻标题）
3. 按 JTBD 框架聚类：当我想 ___ 时，现有方案让我 ___（任务/痛点/期待结果）
4. 统计高频抱怨主题 Top5，每个主题给：
   - 证据条数与来源分布
   - 严重度（是否涉及弃用意愿）
   - 可转化为内容的选题角度

# 输出结构

- JTBD 主题清单（按频次排序，带 insight 引用）
- 可执行选题（映射 mflow.create_content 动作）
