"""Insight Flow 演示数据生成器（让驾驶舱有内容可看）

生成 30 天、覆盖 9 个驾驶舱的连贯演示数据：
metrics 时序（GSC/GA4/CrUX/舆情/旅程/LTV/ CAC/NPS）、洞察（全类型+状态）、
动作与验证（三态结论）、竞品档案+关键词缺口、监控任务、订阅、事件流、报告。

清理：所有演示行带 demo 标记（insights.evidence demo=true / metrics dim demo=true /
competitors 说明前缀 / monitors target demo=true / subscriptions name 前缀 [DEMO]），
`clear()` 只删演示数据，不动真实数据。
"""

import random
from datetime import datetime, timedelta, timezone

from ..core.files import EventBus
from ..core.store import get_store, generate_id

RNG = random.Random(20260916)
TOPICS = ["某品牌", "增长自动化工具", "替代方案"]
STEPS = [("访问定价页", 12000), ("开始试用", 2600), ("完成激活", 900), ("付费转化", 220)]

NEG_WORDS = ["退款", "崩溃", "卡顿", "虚假宣传", "客服差", "扣费", "bug", "失望", "维权", "差评"]


class DemoSeeder:
    """演示数据生成/清理"""

    def __init__(self, workspace_id: str, days: int = 30):
        self.workspace_id = workspace_id
        self.days = days
        self.now = datetime.now(timezone.utc)
        self.bus = EventBus(workspace_id)

    # ================= 生成 =================

    async def seed(self) -> dict:
        await self._metrics()
        insights = await self._insights()
        actions = await self._actions(insights)
        await self._competitors()
        await self._monitors()
        await self._subscriptions()
        await self._journey_events()
        self._events()
        await self._usage()
        reports = await self._reports()
        return {"metrics": "ok", "insights": len(insights), "actions": actions,
                "competitors": 3, "reports": reports}

    def _ts(self, days_ago: float) -> str:
        return (self.now - timedelta(days=days_ago)).isoformat()

    async def _ins_metric(self, store, metric: str, value: float, ts: str,
                          entity: str = "main", entity_type: str = "site",
                          dim: dict | None = None) -> None:
        window_key = str(ts)[:13].replace("T", "-").replace(":", "")[:13] or "w"
        await store._execute(
            """INSERT INTO metrics (id, workspace_id, entity_type, entity_id, metric,
               value, dim_json, ts, monitor_id, window_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'demo', ?)""",
            (generate_id(), self.workspace_id, entity_type, entity, metric,
             float(value), _json({"demo": True, **(dim or {})}), ts, window_key))

    async def _metrics(self) -> None:
        store = await get_store()
        d = self.days
        for i in range(d):
            day = d - i
            wd = (self.now - timedelta(days=day)).weekday()  # 周末低谷
            weekend = 0.75 if wd >= 5 else 1.0
            ts = self._ts(day)
            # 流量：趋势 + 周内波动 + 一次骤降（第 6 天）
            dip = 0.45 if day <= 6 and day >= 4 else 1.0
            clicks = int((3200 + i * 45) * weekend * dip * RNG.uniform(0.92, 1.08))
            impressions = int(clicks * RNG.uniform(11, 15))
            await self._ins_metric(store, "gsc_clicks", clicks, ts)
            await self._ins_metric(store, "gsc_impressions", impressions, ts)
            await self._ins_metric(store, "gsc_ctr", clicks / max(1, impressions), ts)
            await self._ins_metric(store, "ga4_sessions", int(clicks * 1.8 * weekend), ts)
            await self._ins_metric(store, "ga4_conversions",
                                   max(1, int(clicks * 0.021)), ts)
            await self._ins_metric(store, "ga4_retention",
                                   round(0.46 - i * 0.0016 + RNG.uniform(-0.01, 0.01), 4), ts)
            await self._ins_metric(store, "ltv", 2400 + i * 12, ts)
            await self._ins_metric(store, "cac", 480 + i * 3.2, ts)
            await self._ins_metric(store, "nps_score", 42 - i * 0.35, ts)
            # 舆情：三主体
            for j, topic in enumerate(TOPICS):
                neg = round(min(0.62, 0.18 + j * 0.11 + (0.16 if day <= 4 else 0)
                                + RNG.uniform(-0.03, 0.04)), 3)
                await self._ins_metric(store, "topic_negative_ratio", neg, ts,
                                       entity=topic, entity_type="topic",
                                       dim={"counts": {"negative": int(neg * 40), "neutral": 22,
                                                       "positive": 18}})
                await self._ins_metric(store, "topic_sentiment_score",
                                       round(0.25 - neg, 3), ts,
                                       entity=topic, entity_type="topic")
                for ch, base in (("news", 26), ("search", 12), ("reddit", 8)):
                    await self._ins_metric(store, f"topic_mentions_{ch}",
                                           int(base + i * 0.3 + RNG.uniform(0, 4)), ts,
                                           entity=topic, entity_type="topic")
            # 旅程漏斗（人数随时间累积）
            for name, base in STEPS:
                await self._ins_metric(store, "journey_step",
                                       int(base * (0.85 + i / (d * 2.2))), ts,
                                       entity=name, dim={"step_name": name})
        # 竞品定价指标（含一次降价）
        for k, (domain, price, new_price) in enumerate([
                ("competitor-a.com", 99, 99), ("competitor-b.com", 149, 119),
                ("competitor-c.com", 49, 49)]):
            await self._ins_metric(store, "competitor_pricing", new_price, self._ts(3),
                                   entity=domain, entity_type="competitor",
                                   dim={"changed": price != new_price, "old_price": price,
                                        "new_price": new_price})
        # CrUX（最新一次）
        for metric, value in (("crux_lcp", 2.1), ("crux_inp", 180),
                              ("crux_cls", 0.08), ("crux_ttfb", 0.6)):
            await self._ins_metric(store, metric, value, self._ts(1))
        await store._db.commit()

    async def _ins_insight(self, store, **kw) -> dict:
        iid = generate_id()
        created = kw.pop("created_at", self._ts(RNG.uniform(0, self.days)))
        await store._execute(
            """INSERT INTO insights (id, workspace_id, type, title, summary, severity,
               confidence, evidence_json, models_json, actions_json, stage_tags_json,
               status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (iid, self.workspace_id, kw["type"], kw["title"], kw["summary"],
             kw.get("severity", "medium"), kw.get("confidence", 0.75),
             _json(kw.get("evidence", [])), _json(kw.get("models", [])),
             _json(kw.get("actions", [])), _json(kw.get("stage_tags", [])),
             kw.get("status", "new"), created))
        return {"id": iid, "type": kw["type"], "title": kw["title"]}

    async def _insights(self) -> list[dict]:
        store = await get_store()
        out: list[dict] = []
        demo_actions = [{"action_type": "investigate", "description": "复核来源与影响"}]

        # 舆情负面预警（含负面样本 + 命中词，供词云/预警表）
        for j, topic in enumerate(TOPICS):
            examples = [{"text": f"{topic} 产品质量{t}，已经{t2}",
                         "hits": [t, t2]} for t, t2 in
                        [(RNG.choice(NEG_WORDS), RNG.choice(NEG_WORDS)) for _ in range(3)]]
            out.append(await self._ins_insight(store,
                type="topic_negative_alert",
                title=f"舆情负面预警：「{topic}」负向占比 {int((0.36 + j * 0.08) * 100)}%",
                summary=f"监测到 {18 + j * 7} 条负面提及，建议立即响应并归档处置过程。",
                severity="critical" if j == 0 else "high", confidence=0.82,
                evidence=[{"demo": True, "type": "negative_sentiment", "query": topic,
                           "negative_count": 18 + j * 7,
                           "negative_ratio": 0.36 + j * 0.08,
                           "examples": examples}],
                models=["topic_monitor"],
                actions=[{"action_type": "openflow.automation",
                          "description": "触发负面舆情响应流程"},
                         {"action_type": "mflow.create_content", "description": "产出澄清内容"}],
                stage_tags=["S2", "voice", "crisis"],
                created_at=self._ts(2 + j)))
            out.append(await self._ins_insight(store,
                type="topic_digest",
                title=f"「{topic}」全域提及 {420 + j * 130} 条",
                summary="多渠道聚合：news、search、reddit。情绪：正 18 / 中 22 / 负 40。",
                severity="medium", confidence=0.7,
                evidence=[{"demo": True, "type": "topic_aggregate", "query": topic,
                           "channels": {"news": 26, "search": 12, "reddit": 8}}],
                stage_tags=["S0", "voice"], created_at=self._ts(5 + j)))

        # 流量
        out.append(await self._ins_insight(store, type="traffic_anomaly",
            title="流量显著下降（-38%）", summary="最近一期 GSC 点击环比骤降，疑似收录/技术问题。",
            severity="high", confidence=0.8,
            evidence=[{"demo": True, "type": "metric_trend", "metric": "traffic",
                       "before": 3400, "after": 2100, "change_pct": -0.38}],
            models=["aarrr"], actions=demo_actions, stage_tags=["S1", "acquisition"],
            created_at=self._ts(5)))
        out.append(await self._ins_insight(store, type="conversion_low",
            title="转化率偏低（1.8%）", summary="低于行业基准 2%，建议优化定价页与试用引导。",
            severity="medium", confidence=0.7,
            evidence=[{"demo": True, "type": "metric_average", "metric": "conversion",
                       "value": 0.018, "benchmark": 0.02}],
            actions=[{"action_type": "mflow.create_content", "description": "产出转化优化内容"}],
            stage_tags=["S1", "activation"], created_at=self._ts(9)))
        # 关键词机会（散点）
        for k, (kw, imp, pos, ctr) in enumerate([
                ("增长自动化平台", 1800, 8, 0.021), ("竞品分析工具", 1200, 12, 0.03),
                ("saas 增长方案", 900, 15, 0.035), ("营销自动化对比", 700, 7, 0.028),
                ("流量诊断工具", 650, 18, 0.02), ("用户旅程分析", 520, 6, 0.04)]):
            out.append(await self._ins_insight(store, type="keyword_opportunity",
                title=f"关键词机会：{kw}",
                summary=f"「{kw}」曝光 {imp}、排名第 {pos}，CTR 仅 {ctr:.1%}，存在捡漏窗口。",
                severity="medium", confidence=0.72,
                evidence=[{"demo": True, "type": "gsc_query", "keyword": kw,
                           "impressions": imp, "position": pos, "ctr": ctr,
                           "expected_ctr": 0.03}],
                models=["keyword_opportunity"],
                actions=[{"action_type": "mflow.create_content",
                          "description": f"围绕「{kw}」产出内容"}],
                stage_tags=["S1", "seo"], created_at=self._ts(3 + k)))

        # 竞品
        out.append(await self._ins_insight(store, type="competitor_pricing",
            title="竞品 competitor-b.com 降价 20%",
            summary="定价从 $149 调整为 $119（-20%），建议跟进对比内容并评估套餐策略。",
            severity="high", confidence=0.9,
            evidence=[{"demo": True, "type": "pricing_change",
                       "competitor": "competitor-b.com", "old_price": 149,
                       "new_price": 119, "change_pct": -0.201}],
            models=["pricing_watch"],
            actions=[{"action_type": "mflow.create_content", "description": "产出定价对比分析"}],
            stage_tags=["S3", "pricing"], created_at=self._ts(3)))
        out.append(await self._ins_insight(store, type="site_change",
            title="竞品页面大幅变更：competitor-a.com/changelog",
            summary="监控到 changelog 内容变化 34%，疑似发布新模块。",
            severity="medium", confidence=0.7,
            evidence=[{"demo": True, "type": "content_diff",
                       "monitor_id": "demo", "changed_ratio": 0.34,
                       "diff_excerpt": "+ 新增 AI 助手模块"}],
            models=["site_change_monitor"], actions=demo_actions,
            stage_tags=["competitor", "monitoring"], created_at=self._ts(7)))
        out.append(await self._ins_insight(store, type="keyword_gap",
            title="关键词缺口：competitor-b.com 有 12 个词我们没覆盖",
            summary="竞品在 12 个关键词上有排名（我们缺失），Top 机会词：增长自动化平台、竞品分析工具。",
            severity="medium", confidence=0.8,
            evidence=[{"demo": True, "type": "keyword_gap", "competitor": "competitor-b.com",
                       "total_gaps": 12,
                       "top_keywords": [{"keyword": "增长自动化平台", "volume": 1800, "position": 7},
                                        {"keyword": "竞品分析工具", "volume": 1200, "position": 12},
                                        {"keyword": "saas 增长方案", "volume": 900, "position": 9},
                                        {"keyword": "营销自动化对比", "volume": 700, "position": 14},
                                        {"keyword": "流量诊断工具", "volume": 650, "position": 11},
                                        {"keyword": "用户旅程分析", "volume": 520, "position": 8}]}],
            models=["keyword_gap_tracker"],
            actions=[{"action_type": "mflow.create_content", "description": "覆盖缺口词"}],
            stage_tags=["S1", "seo", "competitor"], created_at=self._ts(4)))

        # 旅程 / RFM / NPS / 留存
        out.append(await self._ins_insight(store, type="journey_gap",
            title="旅程断点：开始试用 → 完成激活",
            summary="相邻步转化率仅 34.6%（2600 → 900），存在显著流失断点。",
            severity="high", confidence=0.8,
            evidence=[{"demo": True, "type": "funnel_drop", "from_step": "开始试用",
                       "to_step": "完成激活", "before": 2600, "after": 900,
                       "conversion": 0.346}],
            models=["journey_gap"], actions=demo_actions, stage_tags=["S2", "journey"],
            created_at=self._ts(6)))
        out.append(await self._ins_insight(store, type="journey_content_gap",
            title="旅程空心：Think（考虑）阶段内容覆盖不足",
            summary="该阶段现有触点不足 3 个，补课方向：评测、对比、案例。",
            severity="medium", confidence=0.75,
            evidence=[{"demo": True, "type": "coverage_heatmap", "stage": "think",
                       "stage_name": "Think（考虑）"}],
            models=["journey_coverage"],
            actions=[{"action_type": "mflow.register_topic", "description": "登记缺口选题"}],
            stage_tags=["journey", "content"], created_at=self._ts(11)))
        out.append(await self._ins_insight(store, type="rfm_at_risk",
            title="RFM 流失预警：37 人处于流失边缘",
            summary="at_risk 分群 37 人（占 9%），累计价值 $18,400。建议专属客服回访。",
            severity="high", confidence=0.85,
            evidence=[{"demo": True, "type": "rfm_segments", "segment": "at_risk",
                       "count": 37, "value": 18400}],
            models=["rfm_segmentation"],
            actions=[{"action_type": "openflow.automation", "description": "创建 at_risk 分群"}],
            stage_tags=["S2", "rfm", "retention"], created_at=self._ts(8)))
        out.append(await self._ins_insight(store, type="nps_shift",
            title="NPS 大幅下降（45 → 28）", summary="NPS 出现 ≥10 分异动，建议交叉核对版本发布。",
            severity="high", confidence=0.75,
            evidence=[{"demo": True, "type": "nps_series", "prev": 45, "current": 28}],
            models=["nps"], actions=demo_actions, stage_tags=["S2", "voice_of_customer"],
            created_at=self._ts(10)))
        out.append(await self._ins_insight(store, type="retention_decline",
            title="留存率连续下滑", summary="最近 3 期留存连续下降（46% → 41%），疑似体验变化。",
            severity="high", confidence=0.8,
            evidence=[{"demo": True, "type": "retention_trend",
                       "series": [0.46, 0.44, 0.42, 0.41]}],
            models=["retention_health"], actions=demo_actions,
            stage_tags=["S2", "retention"], created_at=self._ts(12)))
        out.append(await self._ins_insight(store, type="ltv_cac_unhealthy",
            title="LTV:CAC 比值偏低（2.8:1）", summary="低于健康基准 3:1，优先检查渠道 ROI 与留存。",
            severity="medium", confidence=0.85,
            evidence=[{"demo": True, "type": "ratio_calc", "ltv": 2760, "cac": 585,
                       "ratio": 2.82, "benchmark": 3.0}],
            models=["ltv_cac"], actions=demo_actions, stage_tags=["S1", "S3", "unit_economics"],
            created_at=self._ts(13)))

        # 状态分布（闭环看板）
        statuses = ["acknowledged", "actioned", "verified", "dismissed", "new"]
        for i, ins in enumerate(out[:20]):
            if i % 4 == 1:
                await store._execute("UPDATE insights SET status = ? WHERE id = ?",
                                     (statuses[i % len(statuses)], ins["id"]))
        await store._db.commit()
        return out

    async def _actions(self, insights: list[dict]) -> int:
        store = await get_store()
        states = ["dispatched", "done", "verifying", "verified", "verified", "failed", "dead"]
        verdicts = ["effective", "effective", "neutral", "harmful"]
        n = 0
        for i, ins in enumerate(insights[:14]):
            aid = generate_id()
            state = states[i % len(states)]
            dispatched = self._ts(RNG.uniform(1, 20))
            await store._execute(
                """INSERT INTO actions (id, workspace_id, insight_id, action_type, target_ref,
                   params_json, state, dispatched_at, result_json, verify_window_until,
                   baseline_json, created_at)
                   VALUES (?, ?, ?, ?, '', '{}', ?, ?, '{}', ?, ?, ?)""",
                (aid, self.workspace_id, ins["id"],
                 ["mflow.create_content", "openflow.webhook_insight",
                  "feishu.notify", "webhook.generic"][i % 4],
                 state, dispatched,
                 self._ts(RNG.uniform(0, 1)), _json({"demo": True}), dispatched))
            n += 1
            if state == "verified":
                await store._execute(
                    """INSERT INTO feedback (id, workspace_id, action_id, metric, before,
                       after, delta, verdict, evaluated_at)
                       VALUES (?, ?, ?, 'gsc_clicks', ?, ?, ?, ?, ?)""",
                    (generate_id(), self.workspace_id, aid,
                     RNG.uniform(300, 900), RNG.uniform(900, 1500),
                     RNG.uniform(-100, 600),
                     verdicts[i % len(verdicts)], self._ts(RNG.uniform(0, 3))))
        await store._db.commit()
        return n

    async def _competitors(self) -> None:
        store = await get_store()
        for domain, name, pos, pricing in [
                ("competitor-a.com", "竞品 A（头部）", "全栈增长平台，主打企业客户",
                 [{"tier": "Pro", "price": 99}, {"tier": "Team", "price": 249}]),
                ("competitor-b.com", "竞品 B（性价比）", "轻量自动化工具，价格敏感型",
                 [{"tier": "Basic", "price": 119}]),
                ("competitor-c.com", "竞品 C（垂直）", "垂直行业解决方案，客单低",
                 [{"tier": "Starter", "price": 49}])]:
            await store.upsert_competitor(self.workspace_id, domain, {
                "name": name, "positioning": pos, "pricing": pricing,
                "monitors": ["pricing", "changelog"], "seo": {"demo": True}})
        ranks = [{"keyword": k, "position": p, "volume": v, "is_mine": False}
                 for k, p, v in [("增长自动化平台", 7, 1800), ("竞品分析工具", 12, 1200),
                                 ("saas 增长方案", 9, 900), ("营销自动化对比", 14, 700),
                                 ("流量诊断工具", 11, 650), ("用户旅程分析", 8, 520)]]
        await store.save_keyword_ranks(self.workspace_id, "competitor-b.com", ranks)

    async def _monitors(self) -> None:
        store = await get_store()
        for kind, target, cron in [
                ("site_change", {"url": "https://competitor-a.com/pricing", "demo": True}, "0 */6 * * *"),
                ("keyword", {"site": "sc-domain:demo.com", "demo": True}, "0 2 * * *"),
                ("brand_mention", {"query": "增长自动化工具", "demo": True}, "0 */12 * * *"),
                ("topic", {"query": "某品牌", "channels": ["news", "search"], "demo": True}, "0 */6 * * *"),
                ("journey", {"property_id": "123456789", "demo": True}, "0 3 * * *")]:
            await store.create_monitor(self.workspace_id, kind, target, cron)

    async def _subscriptions(self) -> None:
        store = await get_store()
        for name, channels, target, filters, mode in [
                ("[DEMO] 高危舆情告警", ["feishu"],
                 {"feishu_url": "https://open.feishu.cn/open-apis/bot/v2/hook/demo"},
                 {"severity": ["critical", "high"]}, "immediate"),
                ("[DEMO] 客户周报（邮件）", ["email"],
                 {"email_to": "owner@example.com"}, {}, "daily")]:
            await store._execute(
                """INSERT INTO subscriptions (id, workspace_id, name, channels_json,
                   target_json, filters_json, mode, enabled, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                (generate_id(), self.workspace_id, name, _json(channels),
                 _json(target), _json(filters), mode, self._ts(10)))
        await store._db.commit()

    async def _journey_events(self) -> None:
        store = await get_store()
        for i in range(60):
            await store.save_journey_event(
                self.workspace_id, identity=f"demo{i % 12}@example.com",
                stage=["member", "paying", "repeat", "lead"][i % 4],
                event="demo_stage", props={"demo": True, "orders": i % 4},
                ts=self._ts(RNG.uniform(0, 25)))

    def _events(self) -> None:
        for i in range(self.days):
            ts = self._ts(self.days - i)
            self.bus.emit("monitor.run_finished", {"monitor_id": f"demo-{i % 5}"}, ts=ts)
            if i % 6 == 3:
                self.bus.emit("monitor.alert", {"monitor_id": "demo-1",
                                                "error": "HTTP 429 Too Many Requests"}, ts=ts)
            if i % 9 == 4:
                self.bus.emit("quota.exceeded", {"source": "serper", "limit": 20000}, ts=ts)
            if i % 11 == 5:
                self.bus.emit("source.token_refresh_failed",
                              {"provider": "gsc", "error": "invalid_grant"}, ts=ts)

    async def _usage(self) -> None:
        from ..engine.billing import BillingManager
        mgr = BillingManager(self.workspace_id)
        try:
            await mgr.start_trial()   # 演示态：Growth 试用（配额有对比度）
        except Exception:
            pass
        for kind, amount in [("api_calls", 12800), ("agent_asks", 64),
                             ("deep_reports", 3), ("cost_usd", 31.4)]:
            await mgr.record_usage_persisted(kind, amount)

    async def _reports(self) -> int:
        from ..engine.competitor import CompetitorModule
        from ..engine.diagnosis import DiagnosisEngine
        from ..engine.weekly_report import WeeklyReportBuilder
        n = 0
        try:
            await DiagnosisEngine(self.workspace_id).run()
            n += 1
        except Exception:
            pass
        try:
            await WeeklyReportBuilder(self.workspace_id).build()
            n += 1
        except Exception:
            pass
        try:
            await CompetitorModule(self.workspace_id).publish_weekly_report()
            n += 1
        except Exception:
            pass
        return n

    # ================= 清理 =================

    async def clear(self) -> dict:
        store = await get_store()
        counts = {}
        # 按依赖倒序删除（feedback → actions → insights → 其余），避免外键冲突
        for table, cond in [
                ("feedback", "id IN (SELECT f.id FROM feedback f JOIN actions a ON f.action_id = a.id "
                             "WHERE a.baseline_json LIKE '%\"demo\": true%')"),
                ("actions", "baseline_json LIKE '%\"demo\": true%'"),
                ("insights", "evidence_json LIKE '%\"demo\": true%'"),
                ("metrics", "dim_json LIKE '%\"demo\": true%'"),
                ("competitors", "domain LIKE 'competitor-%'"),
                ("monitors", "target_json LIKE '%\"demo\": true%'"),
                ("subscriptions", "name LIKE '[DEMO]%'"),
                ("journey_events", "props_json LIKE '%\"demo\": true%'"),
        ]:
            cur = await store._execute(f"DELETE FROM {table} WHERE workspace_id = ? AND {cond}",
                                       (self.workspace_id,))
            counts[table] = cur.rowcount
        # 事件流（演示期事件无法标记，按时间窗清理）
        counts["events"] = self.bus.rotate(keep_days=0)["dropped"]
        await store._db.commit()
        return counts


def _json(v) -> str:
    import json
    return json.dumps(v, ensure_ascii=False)
