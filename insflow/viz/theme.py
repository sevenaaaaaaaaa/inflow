"""Insight Flow 图表主题（与控制台 CSS 变量同源）

颜色用 CSS 变量引用（var(--accent)），因此明暗主题自动切换；
导出 PDF/打印时同样生效（变量在 base 模板的 :root 内定义）。
"""

# 语义色（CSS 变量名）
ACCENT = "var(--accent)"
OK = "var(--ok)"
WARN = "var(--warn)"
DANGER = "var(--danger)"
MUTED = "var(--muted)"
FAINT = "var(--faint)"
FG = "var(--fg)"
BORDER = "var(--border)"
SURFACE = "var(--surface-strong)"

# 分类色板（多序列）
PALETTE = [
    "var(--accent)",
    "var(--ok)",
    "var(--warn)",
    "var(--danger)",
    "oklch(60% .14 300)",   # 紫
    "oklch(64% .12 200)",   # 青
    "oklch(62% .13 30)",    # 橙
    "oklch(58% .10 130)",   # 绿
]

# 严重度 → 色
SEVERITY_COLOR = {
    "critical": DANGER,
    "high": DANGER,
    "medium": WARN,
    "low": ACCENT,
    "info": MUTED,
}

# 情绪 → 色
SENTIMENT_COLOR = {
    "negative": DANGER,
    "neutral": MUTED,
    "positive": OK,
}


def series_color(i: int) -> str:
    return PALETTE[i % len(PALETTE)]


def severity_color(severity: str) -> str:
    return SEVERITY_COLOR.get(severity, MUTED)
