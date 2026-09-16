---
name: weekly-brief-writer
description: 周报撰写：汇总本周诊断/竞品/舆情洞察生成管理层数据增长简报
triggers: [周报, 简报, weekly, 本周]
inputs: {workspace_id: string}
tools: [query_insights, get_feedback_stats, list_reports]
---

# 执行步骤

1. `query_insights` 拉取最近 7 天全部洞察（含 dismissed，用于统计口径）
2. `get_feedback_stats` 补充动作验证结论（本周验证了哪些假设、命中率）
3. `list_reports` 引用本期诊断/竞品报告文件名
4. 按以下结构输出 Markdown 简报：
   - 本周摘要（3 行以内：最重要的一件事 + 数字）
   - 洞察 Top5（severity 排序，带 id 引用）
   - 验证结果（effective/neutral/harmful 计数）
   - 下周建议（从 insights 的 recommended_actions 汇总）
5. 简报存为文件（报告即文件），并在文末列数据来源 insight id 清单

# 风格要求

- 面向决策者：先结论后证据，每条洞察一句话
- 不编造数字：所有数字必须来自工具返回
