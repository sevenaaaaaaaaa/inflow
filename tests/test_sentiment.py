"""测试情绪分析引擎（G-3）"""

from insflow.engine.sentiment import analyze, distribution


class TestAnalyze:
    def test_negative_chinese(self):
        r = analyze("这个产品太差了，客服态度糟糕，已经申请退款")
        assert r.label == "negative"
        assert r.score < 0
        assert r.negative_hits

    def test_positive_chinese(self):
        r = analyze("非常好用，很稳定，强烈推荐！")
        assert r.label == "positive"
        assert r.score > 0

    def test_english_negative(self):
        r = analyze("Terrible product, it keeps crashing. Worst experience ever.")
        assert r.label == "negative"

    def test_english_positive(self):
        r = analyze("Great tool, reliable and fast. Highly recommend.")
        assert r.label == "positive"

    def test_neutral(self):
        r = analyze("今天发布了新版本")
        assert r.label == "neutral"
        assert r.score == 0.0

    def test_negation_flips(self):
        """否定翻转：不推荐 → 负面"""
        r = analyze("不推荐这个方案")
        assert r.label == "negative"

    def test_intensifier_weight(self):
        """强调词加权"""
        mild = analyze("差")
        strong = analyze("非常差")
        assert abs(strong.score) >= abs(mild.score)

    def test_empty(self):
        r = analyze("")
        assert r.label == "neutral"

    def test_explainability(self):
        """可解释：返回命中词（为什么判定负面）"""
        r = analyze("这个产品垃圾，还泄露用户数据")
        assert "垃圾" in r.negative_hits or "泄露" in r.negative_hits


class TestDistribution:
    def test_counts_and_ratios(self):
        texts = [
            "很好用，推荐",
            "太差了，垃圾产品",
            "今天上线了新功能",
            "质量糟糕，退款了",
        ]
        d = distribution(texts)
        assert d["total"] == 4
        assert d["counts"]["negative"] == 2
        assert d["counts"]["positive"] == 1
        assert d["counts"]["neutral"] == 1
        assert d["ratios"]["negative"] == 0.5
        assert d["avg_score"] < 0

    def test_negative_examples(self):
        d = distribution(["垃圾", "不错"])
        assert d["negative_examples"]
        assert "hits" in d["negative_examples"][0]

    def test_empty(self):
        d = distribution([])
        assert d["total"] == 0
        assert d["ratios"]["negative"] == 0.0
