# 驾驶舱聚合层

每舱一个 `async fn(workspace_id, days) -> dict`，统一经 TTL 缓存（90s）。

性能约定（对齐 docs/10 与 OpenFlow 教训）：
- 只用聚合查询（metrics 走 (ws, metric, ts) 索引）+ LIMIT
- 洞察类数据 limit ≤ 300，Python 侧聚合
- 页面刷新命中缓存，不重复打库；`cache.invalidate("cockpit:<name>")` 可精确失效
