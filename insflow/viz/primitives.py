"""Insight Flow SVG 图表基座（零依赖、服务端渲染）

设计要点（对齐 docs/10 与 OpenFlow 教训）：
- 纯字符串 SVG，无第三方图表库、无 CDN、无构建链
- viewBox + width:100%：响应式；打印/导出 PDF 正常
- 颜色用 CSS 变量：明暗主题自动适配
- 所有函数对空数据/单点数据安全（不抛异常，降级为占位）
"""

import html
from dataclasses import dataclass
from typing import Sequence

from . import theme


def esc(text) -> str:
    return html.escape(str(text if text is not None else ""))


def svg(width: int, height: int, body: str, *, cls: str = "chart",
        aria: str = "") -> str:
    """SVG 外壳：响应式 + 可访问性（role=img + aria-label；无标签用通用描述）"""
    label = aria or "图表（数据表见下方）"
    return (f'<svg class="{cls}" viewBox="0 0 {width} {height}" width="100%" '
            f'height="{height}" role="img" aria-label="{esc(label)}" '
            f'xmlns="http://www.w3.org/2000/svg" '
            f'style="display:block;overflow:visible">{body}</svg>')


def nice_max(value: float, ticks: int = 4) -> float:
    """把最大值向上取整到"好看"的刻度上限"""
    if value is None or value <= 0:
        return 1.0
    import math
    exp = math.floor(math.log10(value))
    base = 10 ** exp
    for mult in (1, 1.5, 2, 2.5, 3, 4, 5, 7.5, 10):
        if value <= base * mult:
            return base * mult
    return base * 10


def fmt_num(v: float) -> str:
    """数字紧凑显示（1.2k / 3.4M）"""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "-"
    for unit, div in (("M", 1_000_000), ("k", 1_000)):
        if abs(v) >= div:
            return f"{v / div:.1f}{unit}".replace(".0", "")
    if abs(v) >= 100:
        return f"{v:.0f}"
    if abs(v) >= 1:
        return f"{v:.1f}".replace(".0", "")
    return f"{v:.2f}".rstrip("0").rstrip(".")


def fmt_pct(v: float) -> str:
    try:
        return f"{float(v) * 100:.0f}%"
    except (TypeError, ValueError):
        return "-"


def line_points(values: Sequence[float], x0: float, y0: float, w: float, h: float,
                y_max: float) -> tuple[str, list[tuple[float, float]]]:
    """数值序列 → SVG polyline 点串 + 坐标点列表"""
    n = len(values)
    if n == 0:
        return "", []
    if n == 1:
        pts = [(x0 + w / 2, y0 + h - (values[0] / y_max) * h)]
        return f"{pts[0][0]:.1f},{pts[0][1]:.1f}", pts
    pts = []
    for i, v in enumerate(values):
        x = x0 + w * i / (n - 1)
        y = y0 + h - (max(0.0, float(v)) / y_max) * h
        pts.append((x, y))
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in pts), pts


def axis_labels(labels: Sequence[str], x0: float, y0: float, w: float,
                max_labels: int = 6) -> str:
    """X 轴标签（自动抽稀，避免拥挤）"""
    n = len(labels)
    if n == 0:
        return ""
    step = max(1, n // max_labels)
    parts = []
    for i in range(0, n, step):
        x = x0 + (w * i / (n - 1) if n > 1 else w / 2)
        parts.append(f'<text x="{x:.1f}" y="{y0 + 14}" text-anchor="middle" '
                     f'style="font-size:10px;fill:{theme.FAINT}">{esc(labels[i][:8])}</text>')
    return "".join(parts)


def y_grid(y_max: float, x0: float, y0: float, w: float, h: float,
           ticks: int = 4) -> str:
    """横向网格线 + Y 轴刻度"""
    parts = []
    for i in range(ticks + 1):
        v = y_max * i / ticks
        y = y0 + h - h * i / ticks
        parts.append(f'<line x1="{x0:.1f}" y1="{y:.1f}" x2="{x0 + w:.1f}" y2="{y:.1f}" '
                     f'style="stroke:{theme.BORDER};stroke-width:1;'
                     f'stroke-dasharray:{"0" if i == 0 else "3 3"}"/>')
        parts.append(f'<text x="{x0 - 6:.1f}" y="{y + 3:.1f}" text-anchor="end" '
                     f'style="font-size:10px;fill:{theme.FAINT}">{fmt_num(v)}</text>')
    return "".join(parts)


def legend(items: Sequence[tuple[str, str]], x: float, y: float,
           *, interactive: bool = True) -> str:
    """图例（名称 + 色块）；interactive=True 时可点击开关序列（a11y：role=button）"""
    parts = []
    cx = x
    for name, color in items:
        label = str(name)
        rect = (f'<rect x="{cx:.1f}" y="{y - 8:.1f}" width="9" height="9" rx="2" '
                f'fill="{color}"/>')
        text = (f'<text x="{cx + 13:.1f}" y="{y:.1f}" '
                f'style="font-size:10.5px;fill:{theme.MUTED}">{esc(label)}</text>')
        if interactive:
            w = 22 + len(label) * 8
            parts.insert(0, "")  # no-op 占位（保持结构可读）
            parts.append(
                f'<g class="if-legend" data-series="{esc(label)}" role="button" '
                f'tabindex="0" aria-pressed="true" aria-label="切换 {esc(label)} 显示" '
                f'onclick="ifToggleSeries(\'{esc(label)}\', this)" '
                f'onkeydown="ifLegendKey(event, \'{esc(label)}\', this)">{rect}{text}</g>')
            cx += w
        else:
            parts.append(rect); parts.append(text)
            cx += 22 + len(label) * 8
    return "".join(parts)


def placeholder(width: int, height: int, text: str = "暂无数据") -> str:
    """空数据占位（统一风格，不抛异常）"""
    return svg(width, height,
               f'<text x="{width / 2}" y="{height / 2}" text-anchor="middle" '
               f'style="font-size:12px;fill:{theme.FAINT}">{esc(text)}</text>')
