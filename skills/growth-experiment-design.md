---
name: growth-experiment-design
description: 增长实验设计：把洞察转化为 ICE 评分的实验方案（假设/指标/样本量）
triggers: [实验, experiment, 测试, A/B, 增长实验]
inputs: {insight_id: string, horizon_days: int = 14}
tools: [get_insight_detail, get_feedback_stats, query_insights]
---

# 执行步骤

1. `get_insight_detail` 读取洞察的证据与推荐动作
2. 把"推荐动作"改写为可证伪的实验假设：如果 ___（干预），那么 ___（指标）将提升 ___%
3. 用 `get_feedback_stats` 参考历史验证结论，校准预期提升幅度
4. 按 ICE 打分：Impact（影响）/ Confidence（置信）/ Ease（实施难度）
5. 输出实验卡：主指标 / 护栏指标 / 样本量估算 / 运行周期 / 判停规则

# 约束

- 实验周期与 IF 验证窗口（默认 14 天）对齐
- 每个实验必须绑定一个可回流的指标（GSC 点击 / GA4 转化）
