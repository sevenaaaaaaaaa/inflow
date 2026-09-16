"""Insight Flow 情绪分析引擎（G-3 舆情强化）

确定性词典法（无外部依赖、可解释、可测试）：
- 中英双语情感词典（面向品牌/舆情场景）
- 否定词翻转（不/没/无/not/never...）
- 强调词加权（非常/极其/太/very/extremely...）
- 返回命中词（可解释性：为什么判定为负面）

适用：品牌词/话题提及的情绪分布、负面预警、舆情趋势。
"""

import re
from dataclasses import dataclass, field

# 情感词典（负向）
NEGATIVE = {
    # 中文
    "差", "差劲", "垃圾", "坑", "踩坑", "骗", "欺骗", "诈骗", "虚假", "造假", "失望",
    "投诉", "退款", "退货", "崩", "崩溃", "卡顿", "宕机", "故障", "泄露", "事故",
    "愤怒", "抵制", "差评", "退订", "倒闭", "跑路", "恶心", "糟糕", "拉黑", "避雷",
    "不推荐", "不靠谱", "翻车", "维权", "抄袭", "侵权", "下架", "封号", "扣费",
    "割韭菜", "智商税", "套路", "霸王条款", "歧视", "辱骂",
    # 英文
    "bad", "terrible", "awful", "scam", "fraud", "broken", "crash", "bug", "bugs",
    "disappointed", "disappointing", "refund", "hate", "worst", "angry", "leak",
    "lawsuit", "sue", "downtime", "outage", "unreliable", "overpriced", "ripoff",
    "cancel", "boycott", "toxic", "shameful",
}

# 情感词典（正向）
POSITIVE = {
    # 中文
    "好", "优秀", "推荐", "喜欢", "满意", "赞", "稳定", "高效", "值得", "惊喜",
    "靠谱", "强大", "流畅", "专业", "超值", "好评", "支持", "良心", "用心",
    "给力", "好评如潮", "性价比高", "上手快", "省心", "必买", "回购",
    # 英文
    "good", "great", "excellent", "awesome", "love", "recommend", "stable",
    "fast", "reliable", "worth", "amazing", "smooth", "solid", "impressive",
    "delighted", "value", "best",
}

# 否定词（命中后翻转其后情感词）
NEGATIONS = {"不", "没", "没有", "无", "非", "别", "未", "难以", "无法",
             "not", "no", "never", "without", "hardly", "barely"}

# 强调词（加权）
INTENSIFIERS = {"非常": 1.6, "极其": 1.8, "太": 1.5, "超": 1.5, "特别": 1.5,
                "十分": 1.5, "很": 1.3, "蛮": 1.3, "巨": 1.6,
                "very": 1.5, "extremely": 1.8, "really": 1.4, "so": 1.3, "super": 1.5}

POSITIVE_THRESHOLD = 0.2
NEGATIVE_THRESHOLD = -0.2


@dataclass
class SentimentResult:
    score: float                 # -1..1
    label: str                   # negative | neutral | positive
    positive_hits: list[str] = field(default_factory=list)
    negative_hits: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"score": round(self.score, 3), "label": self.label,
                "positive_hits": self.positive_hits[:8],
                "negative_hits": self.negative_hits[:8]}


def _tokenize(text: str) -> list[str]:
    """粗切分：英文按词、中文按字保留（词典为词/短语，直接子串匹配更稳）"""
    return re.findall(r"[A-Za-z]+|[\u4e00-\u9fff]", text or "")


def analyze(text: str) -> SentimentResult:
    """单条文本情绪分析"""
    if not text or not text.strip():
        return SentimentResult(score=0.0, label="neutral")
    lowered = text.lower()

    pos_hits: list[str] = []
    neg_hits: list[str] = []
    pos_weight = 0.0
    neg_weight = 0.0

    for term in POSITIVE:
        if term_matches(term, lowered):
            weight = term_weight(term, lowered)
            if negated(term, lowered):
                neg_hits.append(term)
                neg_weight += weight
            else:
                pos_hits.append(term)
                pos_weight += weight

    for term in NEGATIVE:
        if term_matches(term, lowered):
            weight = term_weight(term, lowered)
            if negated(term, lowered):
                pos_hits.append(term)
                pos_weight += weight
            else:
                neg_hits.append(term)
                neg_weight += weight

    total = pos_weight + neg_weight
    if total == 0:
        return SentimentResult(score=0.0, label="neutral")
    score = (pos_weight - neg_weight) / total
    if score >= POSITIVE_THRESHOLD:
        label = "positive"
    elif score <= NEGATIVE_THRESHOLD:
        label = "negative"
    else:
        label = "neutral"
    return SentimentResult(score=score, label=label,
                           positive_hits=pos_hits, negative_hits=neg_hits)


def term_matches(term: str, lowered_text: str) -> bool:
    if re.fullmatch(r"[a-z]+", term):
        return re.search(rf"\b{re.escape(term)}\b", lowered_text) is not None
    return term in lowered_text


def term_weight(term: str, lowered_text: str) -> float:
    """命中点附近的强调词加权（±6 字符窗口）"""
    idx = lowered_text.find(term)
    if idx < 0:
        return 1.0
    window = lowered_text[max(0, idx - 6): idx]
    weight = 1.0
    for word, factor in INTENSIFIERS.items():
        if word in window:
            weight = max(weight, factor)
    return weight


def negated(term: str, lowered_text: str) -> bool:
    """词前 4 字符内出现否定词 → 视为翻转"""
    idx = lowered_text.find(term)
    if idx <= 0:
        return False
    window = lowered_text[max(0, idx - 4): idx]
    return any(neg in window for neg in NEGATIONS)


def distribution(texts: list[str]) -> dict:
    """批量情绪分布（舆情概览的核心指标）"""
    counts = {"positive": 0, "neutral": 0, "negative": 0}
    scores: list[float] = []
    negative_examples: list[dict] = []

    for t in texts:
        r = analyze(t)
        counts[r.label] += 1
        scores.append(r.score)
        if r.label == "negative" and r.negative_hits:
            negative_examples.append({"text": (t or "")[:80],
                                      "hits": r.negative_hits[:5]})

    total = len(texts)
    return {
        "total": total,
        "counts": counts,
        "ratios": {k: round(v / total, 3) if total else 0.0 for k, v in counts.items()},
        "avg_score": round(sum(scores) / total, 3) if total else 0.0,
        "negative_examples": negative_examples[:5],
    }
