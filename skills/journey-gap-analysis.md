---
name: journey-gap-analysis
description: 客户旅程断点分析：从旅程与漏斗洞察中定位流失最严重的步骤并给修复建议
triggers: [旅程, 旅程, journey, 断点, 漏斗]
inputs: {journey_id: string = "main"}
tools: [query_insights, get_insight_detail, list_reports]
---

# 执行步骤

1. `query_insights` 检索 journey_gap / conversion_low 类洞察
2. `get_insight_detail` 读取 funnel_drop 证据（相邻步转化率）
3. 按"断点严重度 × 流量规模"排序（损失人数 = before × (1 - conversion)）
4. 对 Top3 断点给修复假设：文案疑虑 / 流程摩擦 / 信任缺失 / 加载性能
5. 引用 insight id，并把修复动作映射到 openflow.automation（分群旅程）或 mflow.create_content

# 注意

- 修复建议必须引用证据数字，不得凭空推断
- 大流量低转化的断点优先于高转化率的精细优化
