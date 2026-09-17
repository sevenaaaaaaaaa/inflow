"""Insight Flow SVG 图表集（服务端渲染，零依赖）

按 docs/10 图表语言规范实现：单值→KPI｜趋势→折线/面积｜分类→横向条形｜
构成→堆叠条/百分比条｜漏斗→funnel｜交叉→heatmap｜机会→scatter｜5维→radar｜
进度→gauge｜事件→timeline｜热度→tag_cloud
"""

from typing import Sequence

from . import theme
from .primitives import (
    axis_labels,
    esc,
    fmt_num,
    fmt_pct,
    legend,
    line_points,
    nice_max,
    placeholder,
    svg,
    y_grid,
)


# ========== KPI 卡（HTML，非 SVG）==========

def kpi_card(label: str, value, *, delta: float | None = None,
             spark: Sequence[float] | None = None, hint: str = "",
             color: str = theme.ACCENT) -> str:
    """单值 + 环比 + 迷你走势"""
    delta_html = ""
    if delta is not None:
        up = delta >= 0
        dcol = theme.OK if up else theme.DANGER
        arrow = "▲" if up else "▼"
        delta_html = (f'<span style="font-size:11.5px;color:{dcol};font-weight:700">'
                      f'{arrow} {abs(delta) * 100:.0f}%</span>')
    spark_html = ""
    if spark:
        spark_html = (f'<div style="margin-top:6px">'
                      f'{sparkline(list(spark), width=120, height=26)}</div>')
    hint_html = (f'<div style="font-size:11px;color:var(--faint);margin-top:4px">{esc(hint)}</div>'
                 if hint else "")
    return (f'<div class="card kpi"><div class="num" style="color:{color}">{esc(value)}</div>'
            f'<div class="label">{esc(label)} {delta_html}</div>{spark_html}{hint_html}</div>')


def sparkline(values: Sequence[float], *, width: int = 120, height: int = 26,
              color: str = theme.ACCENT, fill: bool = True) -> str:
    if not values:
        return placeholder(width, height)
    y_max = nice_max(max(values) or 1)
    pts, coords = line_points(values, 1, 2, width - 2, height - 6, y_max)
    area = ""
    if fill and coords:
        area = (f'<polygon points="{coords[0][0]:.1f},{height - 4} {pts} '
                f'{coords[-1][0]:.1f},{height - 4}" fill="{color}" opacity="0.12"/>')
    return svg(width, height, f'{area}<polyline points="{pts}" fill="none" '
               f'stroke="{color}" stroke-width="1.6" stroke-linejoin="round"/>')


# ========== 折线 / 面积（趋势）==========

def line_chart(series: list[dict], labels: Sequence[str], *, width: int = 720,
               height: int = 220, y_label: str = "", as_area: bool = False) -> str:
    """多序列趋势（series: [{name, values, color?}]）"""
    if not series or not any(s.get("values") for s in series):
        return placeholder(width, height)

    pad_l, pad_r, pad_t, pad_b = 44, 12, 14, 26
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    y_max = nice_max(max((max(s["values"]) if s["values"] else 0) for s in series))

    body = [y_grid(y_max, pad_l, pad_t, plot_w, plot_h)]
    legend_items = []
    for i, s in enumerate(series):
        color = s.get("color") or theme.series_color(i)
        legend_items.append((s["name"], color))
        pts, coords = line_points(list(s["values"]), pad_l, pad_t, plot_w, plot_h, y_max)
        if as_area and coords:
            body.append(f'<polygon points="{coords[0][0]:.1f},{pad_t + plot_h} {pts} '
                        f'{coords[-1][0]:.1f},{pad_t + plot_h}" fill="{color}" opacity="0.10"/>')
        body.append(f'<polyline points="{pts}" fill="none" stroke="{color}" '
                    f'stroke-width="2" stroke-linejoin="round"/>')
        if len(coords) <= 40:  # 点少时标点，便于 hover
            for x, y in coords:
                body.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.2" fill="{color}"/>')
    body.append(axis_labels(labels, pad_l, pad_t + plot_h, plot_w))
    if len(series) > 1:
        body.append(legend(legend_items, pad_l, 10))
    if y_label:
        body.append(f'<text x="{pad_l}" y="{pad_t - 4}" style="font-size:10px;'
                    f'fill:{theme.FAINT}">{esc(y_label)}</text>')
    return svg(width, height, "".join(body))


# ========== 横向条形（分类对比）==========

def bar_chart(items: Sequence[tuple[str, float]], *, width: int = 720,
              height: int | None = None, color: str = theme.ACCENT,
              threshold: float | None = None, threshold_label: str = "",
              value_fmt=fmt_num, colors: Sequence[str] | None = None) -> str:
    """横向条形（名称长时优于柱状）；可选阈值参考线"""
    items = [(str(k), float(v or 0)) for k, v in items]
    if not items:
        return placeholder(width, 120)
    row_h = 24
    height = height or max(80, 14 + row_h * len(items) + 8)
    label_w = min(180, max(80, int(width * 0.28)))
    bar_w = width - label_w - 70
    v_max = nice_max(max(v for _, v in items) or 1)

    body = []
    for i, (label, value) in enumerate(items):
        y = 8 + i * row_h
        w = max(1.0, bar_w * value / v_max)
        c = (colors[i % len(colors)] if colors else color)
        body.append(f'<text x="0" y="{y + 13}" style="font-size:11.5px;fill:{theme.FG}">'
                    f'{esc(label[:22])}</text>')
        body.append(f'<rect x="{label_w}" y="{y + 3}" width="{bar_w}" height="12" rx="6" '
                    f'fill="{theme.BORDER}" opacity="0.5"/>')
        body.append(f'<rect x="{label_w}" y="{y + 3}" width="{w:.1f}" height="12" rx="6" fill="{c}"/>')
        body.append(f'<text x="{label_w + bar_w + 8}" y="{y + 13}" '
                    f'style="font-size:11px;fill:{theme.MUTED};font-weight:600">'
                    f'{esc(value_fmt(value))}</text>')
    if threshold is not None and v_max:
        tx = label_w + bar_w * threshold / v_max
        body.append(f'<line x1="{tx:.1f}" y1="6" x2="{tx:.1f}" y2="{height - 6}" '
                    f'stroke="{theme.DANGER}" stroke-width="1.4" stroke-dasharray="4 3"/>')
        if threshold_label:
            body.append(f'<text x="{tx + 4:.1f}" y="12" style="font-size:9.5px;'
                        f'fill:{theme.DANGER}">{esc(threshold_label)}</text>')
    return svg(width, height, "".join(body))


# ========== 构成（堆叠条 / 百分比条）==========

def percent_bar(parts: Sequence[tuple[str, float, str]], *, width: int = 720,
                height: int = 26, show_legend: bool = True) -> str:
    """构成占比（parts: [(name, value, color)]）——≤6 类时优于饼图"""
    total = sum(max(0.0, float(v)) for _, v, _ in parts)
    if total <= 0:
        return placeholder(width, height)
    body, x = [], 0.0
    for name, value, color in parts:
        w = width * max(0.0, float(value)) / total
        if w <= 0:
            continue
        pct = max(0.0, float(value)) / total
        body.append(f'<rect x="{x:.1f}" y="0" width="{max(0.5, w):.1f}" height="{height}" '
                    f'fill="{color}"/>')
        if w > 34:  # 太窄不放字
            body.append(f'<text x="{x + w / 2:.1f}" y="{height / 2 + 4:.1f}" '
                        f'text-anchor="middle" style="font-size:10.5px;fill:'
                        f'{theme.SURFACE if False else "oklch(100% 0 0)"}">{pct:.0%}</text>')
        x += w
    out = svg(width, height, "".join(body))
    if show_legend:
        out += (f'<div style="font-size:11px;color:{theme.MUTED};margin-top:6px">'
                + " · ".join(f'<span style="color:{c};font-weight:700">■</span> {esc(n)} '
                             f'{fmt_num(v)}（{fmt_pct(float(v) / total)}）'
                             for n, v, c in parts) + "</div>")
    return out


def stacked_bar(rows: Sequence[tuple[str, list[tuple[str, float, str]]]], *,
                width: int = 720, height: int = 200) -> str:
    """按行堆叠柱（如各渠道按周构成）：rows=[(label, [(name,value,color)])]"""
    if not rows:
        return placeholder(width, height)
    pad_l, pad_r, pad_t, pad_b = 44, 12, 14, 26
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    totals = [sum(max(0.0, v) for _, v, _ in parts) for _, parts in rows]
    y_max = nice_max(max(totals) or 1)
    n = len(rows)
    bar_w = max(3.0, plot_w / max(1, n) * 0.62)
    gap = plot_w / max(1, n)

    body = [y_grid(y_max, pad_l, pad_t, plot_w, plot_h)]
    for i, (_, parts) in enumerate(rows):
        x = pad_l + gap * i + (gap - bar_w) / 2
        y_cursor = pad_t + plot_h
        for _, value, color in parts:
            h = plot_h * max(0.0, float(value)) / y_max
            if h <= 0:
                continue
            y_cursor -= h
            body.append(f'<rect x="{x:.1f}" y="{y_cursor:.1f}" width="{bar_w:.1f}" '
                        f'height="{h:.1f}" fill="{color}"/>')
    body.append(axis_labels([r[0] for r in rows], pad_l, pad_t + plot_h, plot_w))
    names = [nm for nm, _, _ in rows[0][1]] if rows and rows[0][1] else []
    if names:
        colors = [c for _, _, c in rows[0][1]]
        body.append(legend(list(zip(names, colors)), pad_l, 10))
    return svg(width, height, "".join(body))


# ========== 漏斗（旅程 / 转化）==========

def funnel(steps: Sequence[tuple[str, float]], *, width: int = 720,
           height: int = 240) -> str:
    """漏斗图（相邻步转化率标注）"""
    steps = [(str(k), float(v or 0)) for k, v in steps]
    if not steps:
        return placeholder(width, height)
    top = steps[0][1] or 1
    n = len(steps)
    label_h = 22
    band_h = max(24, (height - 10) / n - 6)
    body = []
    for i, (label, value) in enumerate(steps):
        ratio = value / top
        w = max(6.0, (width - 200) * ratio)
        x = (width - 200 - w) / 2 + 8
        y = 6 + i * (band_h + 6)
        color = theme.series_color(i)
        body.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{band_h:.1f}" '
                    f'rx="6" fill="{color}" opacity="0.86"/>')
        body.append(f'<text x="{x + w / 2:.1f}" y="{y + band_h / 2 + 4:.1f}" text-anchor="middle" '
                    f'style="font-size:11px;fill:oklch(100% 0 0)">{fmt_num(value)}</text>')
        body.append(f'<text x="{width - 186}" y="{y + band_h / 2 + 4:.1f}" '
                    f'style="font-size:11.5px;fill:{theme.FG}">{esc(label[:24])} · '
                    f'{fmt_pct(ratio)}</text>')
        if i > 0:
            prev = steps[i - 1][1] or 1
            conv = value / prev if prev else 0
            warn = conv < 0.35
            body.append(f'<text x="2" y="{y + 6:.1f}" style="font-size:10px;fill:'
                        f'{theme.DANGER if warn else theme.FAINT};font-weight:'
                        f'{"700" if warn else "400"}">↓ {conv:.0%}</text>')
    return svg(width, height, "".join(body))


# ========== 热力矩阵（双维交叉）==========

def heatmap(rows: Sequence[str], cols: Sequence[str], matrix: Sequence[Sequence[float]], *,
            width: int = 720, height: int | None = None,
            row_w: int = 120) -> str:
    """热力矩阵（如旅程阶段 × 触点数、时段 × 渠道）"""
    if not rows or not cols:
        return placeholder(width, height or 160)
    cell_w = max(18, (width - row_w - 8) / len(cols))
    cell_h = 26
    height = height or max(80, 20 + cell_h * len(rows))
    body = []
    for j, c in enumerate(cols):
        body.append(f'<text x="{row_w + cell_w * j + cell_w / 2:.1f}" y="12" '
                    f'text-anchor="middle" style="font-size:10px;fill:{theme.FAINT}">'
                    f'{esc(str(c)[:6])}</text>')
    for i, r in enumerate(rows):
        y = 18 + i * cell_h
        body.append(f'<text x="0" y="{y + cell_h / 2 + 4:.1f}" style="font-size:11px;'
                    f'fill:{theme.FG}">{esc(str(r)[:16])}</text>')
        for j in range(len(cols)):
            v = matrix[i][j] if i < len(matrix) and j < len(matrix[i]) else 0
            v = max(0.0, min(1.0, float(v)))
            opacity = 0.12 + 0.88 * v
            body.append(f'<rect x="{row_w + cell_w * j + 1:.1f}" y="{y:.1f}" '
                        f'width="{cell_w - 2:.1f}" height="{cell_h - 4}" rx="4" '
                        f'fill="{theme.ACCENT}" opacity="{opacity:.2f}"/>')
    return svg(width, height, "".join(body))


# ========== 散点四象限（机会矩阵）==========

def scatter(points: Sequence[tuple[float, float, str]], *, x_label: str = "",
            y_label: str = "", width: int = 720, height: int = 300,
            x_mid: float | None = None, y_mid: float | None = None,
            good_quadrant: str = "tr") -> str:
    """散点（如关键词机会：搜索量 × 排名/竞争力），支持四象限标注"""
    pts = [(float(x or 0), float(y or 0), str(l)) for x, y, l in points]
    if not pts:
        return placeholder(width, height)
    pad_l, pad_r, pad_t, pad_b = 46, 14, 16, 28
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    x_max = nice_max(max(p[0] for p in pts) or 1)
    y_max = nice_max(max(p[1] for p in pts) or 1)
    xm = x_mid if x_mid is not None else x_max / 2
    ym = y_mid if y_mid is not None else y_max / 2
    body = [y_grid(y_max, pad_l, pad_t, plot_w, plot_h)]

    # 象限分割线 + 高亮"好"象限
    gx = pad_l + plot_w * min(1.0, xm / x_max)
    gy = pad_t + plot_h - plot_h * min(1.0, ym / y_max)
    body.append(f'<rect x="{gx:.1f}" y="{pad_t}" width="{pad_l + plot_w - gx:.1f}" '
                f'height="{gy - pad_t:.1f}" fill="{theme.OK}" opacity="0.07"/>')
    body.append(f'<line x1="{gx:.1f}" y1="{pad_t}" x2="{gx:.1f}" y2="{pad_t + plot_h}" '
                f'stroke="{theme.BORDER}"/>')
    body.append(f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{pad_l + plot_w}" y2="{gy:.1f}" '
                f'stroke="{theme.BORDER}"/>')
    for x, y, label in pts:
        cx = pad_l + plot_w * min(1.0, x / x_max)
        cy = pad_t + plot_h - plot_h * min(1.0, y / y_max)
        color = theme.OK if ((x >= xm) == (good_quadrant in ("tr", "br")) and
                             (y >= ym) == (good_quadrant in ("tl", "tr"))) else theme.ACCENT
        body.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4.5" fill="{color}" opacity="0.85">'
                    f'<title>{esc(label)}（{fmt_num(x)}, {fmt_num(y)}）</title></circle>')
    if x_label:
        body.append(f'<text x="{pad_l + plot_w / 2:.1f}" y="{height - 4}" text-anchor="middle" '
                    f'style="font-size:10.5px;fill:{theme.MUTED}">{esc(x_label)}</text>')
    if y_label:
        body.append(f'<text x="{pad_l}" y="{pad_t - 5}" style="font-size:10.5px;'
                    f'fill:{theme.MUTED}">{esc(y_label)}</text>')
    return svg(width, height, "".join(body))


# ========== 雷达（多维评分）==========

def radar(axes: Sequence[tuple[str, float]], *, width: int = 320, height: int = 260,
          color: str = theme.ACCENT) -> str:
    """雷达图（axes: [(label, 0..1)]）——成熟度五维"""
    axes = [(str(k), max(0.0, min(1.0, float(v)))) for k, v in axes]
    if not axes:
        return placeholder(width, height)
    import math
    cx, cy, R = width / 2, height / 2 + 6, min(width, height) / 2 - 40
    n = len(axes)
    body = []
    for ring in (0.25, 0.5, 0.75, 1.0):
        pts = []
        for i in range(n):
            a = -math.pi / 2 + 2 * math.pi * i / n
            pts.append(f"{cx + R * ring * math.cos(a):.1f},{cy + R * ring * math.sin(a):.1f}")
        body.append(f'<polygon points="{" ".join(pts)}" fill="none" '
                    f'stroke="{theme.BORDER}" stroke-width="1"/>')
    for i, (label, _) in enumerate(axes):
        a = -math.pi / 2 + 2 * math.pi * i / n
        body.append(f'<line x1="{cx}" y1="{cy}" x2="{cx + R * math.cos(a):.1f}" '
                    f'y2="{cy + R * math.sin(a):.1f}" stroke="{theme.BORDER}"/>')
        lx, ly = cx + (R + 16) * math.cos(a), cy + (R + 16) * math.sin(a)
        body.append(f'<text x="{lx:.1f}" y="{ly + 3:.1f}" text-anchor="middle" '
                    f'style="font-size:10px;fill:{theme.MUTED}">{esc(label[:8])}</text>')
    pts = []
    for i, (_, v) in enumerate(axes):
        a = -math.pi / 2 + 2 * math.pi * i / n
        pts.append(f"{cx + R * v * math.cos(a):.1f},{cy + R * v * math.sin(a):.1f}")
    body.append(f'<polygon points="{" ".join(pts)}" fill="{color}" opacity="0.28" '
                f'stroke="{color}" stroke-width="2"/>')
    return svg(width, height, "".join(body))


# ========== 进度环（配额 / 命中率）==========

def gauge(value: float, label: str = "", *, size: int = 96,
          color: str | None = None) -> str:
    """环形进度（value 0..1；>0.8 自动转警示色）"""
    v = max(0.0, min(1.0, float(value or 0)))
    c = color or (theme.DANGER if v >= 0.9 else theme.WARN if v >= 0.8 else theme.ACCENT)
    import math
    r = size / 2 - 8
    cx = cy = size / 2
    circ = 2 * math.pi * r
    body = [
        f'<circle cx="{cx}" cy="{cy}" r="{r:.1f}" fill="none" stroke="{theme.BORDER}" stroke-width="8"/>',
        f'<circle cx="{cx}" cy="{cy}" r="{r:.1f}" fill="none" stroke="{c}" stroke-width="8" '
        f'stroke-linecap="round" stroke-dasharray="{circ * v:.1f} {circ:.1f}" '
        f'transform="rotate(-90 {cx} {cy})"/>',
        f'<text x="{cx}" y="{cy + 4}" text-anchor="middle" style="font-size:14px;'
        f'font-weight:700;fill:{theme.FG}">{fmt_pct(v)}</text>',
    ]
    if label:
        body.append(f'<text x="{cx}" y="{size - 2}" text-anchor="middle" '
                    f'style="font-size:9.5px;fill:{theme.FAINT}">{esc(label[:10])}</text>')
    return svg(size, size + 10, "".join(body))


# ========== 时间线 / 标签云（HTML）==========

def timeline(items: Sequence[dict], *, max_items: int = 20) -> str:
    """事件时间线（items: [{ts, title, severity?, meta?}]）"""
    if not items:
        return '<div class="empty">暂无事件</div>'
    rows = []
    for it in list(items)[:max_items]:
        color = theme.severity_color(str(it.get("severity", "info")))
        ts = str(it.get("ts", ""))[:16].replace("T", " ")
        meta = it.get("meta", "")
        rows.append(
            f'<div style="display:flex;gap:10px;padding:7px 0;border-bottom:1px solid {theme.BORDER}">'
            f'<span style="color:{theme.FAINT};font-size:11px;min-width:92px">{esc(ts)}</span>'
            f'<span style="width:8px;height:8px;border-radius:50%;background:{color};'
            f'margin-top:6px;flex-shrink:0"></span>'
            f'<span style="flex:1;font-size:12.5px">{esc(it.get("title", ""))}'
            + (f'<div style="font-size:11px;color:{theme.FAINT};margin-top:2px">{esc(meta)}</div>'
               if meta else "")
            + "</span></div>")
    return '<div style="font-size:12.5px">' + "".join(rows) + "</div>"


def tag_cloud(tags: Sequence[tuple[str, float]], *, max_items: int = 30,
              color: str = theme.DANGER) -> str:
    """标签条云（字号权重；比纯词云可读）"""
    if not tags:
        return '<div class="empty">暂无高频词</div>'
    tags = list(tags)[:max_items]
    vmax = max((float(w) for _, w in tags), default=1) or 1
    spans = []
    for text, weight in tags:
        ratio = float(weight) / vmax
        size = 11 + 7 * ratio
        spans.append(
            f'<span style="display:inline-block;margin:3px 5px 3px 0;padding:3px 9px;'
            f'border-radius:999px;background:var(--hover-strong);color:{color};'
            f'font-size:{size:.1f}px;font-weight:{700 if ratio > 0.6 else 500}">'
            f'{esc(str(text)[:14])} <span style="color:{theme.FAINT};font-weight:400">'
            f'{int(weight)}</span></span>')
    return '<div style="line-height:1.9">' + "".join(spans) + "</div>"
