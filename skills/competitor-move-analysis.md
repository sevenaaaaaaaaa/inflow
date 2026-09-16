---
name: competitor-move-analysis
description: 竞品异动深度分析：输入竞品与时间窗，输出动机推断与我方应对建议
triggers: [竞品, competitor, 异动, 对手]
inputs: {competitor: string, window_days: int = 7}
tools: [query_insights, get_insight_detail, list_reports]
output_template: reports/templates/competitor-move.md
---

# 执行步骤

1. 用 `query_insights` 拉取该竞品相关洞察（type 含 competitor_* / site_change，按时间窗过滤）
2. 逐条调用 `get_insight_detail` 获取证据链（定价 diff / 内容变化 / 讨论量）
3. 按《动机分类法》归类每次异动：定价 / 产品 / 渠道 / 组织
4. 对每个异动给出：影响评估（高/中/低）+ 置信度 + 我方动作建议（引用洞察里的 recommended_actions）
5. 输出必须引用 insight id 作为证据锚点（如 `[证据: ins_xxx]`），否则质量门会阻断

# 输出结构

- 异动清单（时间线）
- 动机推断（每条带证据引用）
- 我方应对建议（Top3，映射 Action Router 动作类型）
