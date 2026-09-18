"""Insight Flow SVG 图表集（服务端渲染，零依赖）

按 docs/10 图表语言规范实现：单值→KPI｜趋势→折线/面积｜分类→横向条形｜
构成→堆叠条/百分比条｜漏斗→funnel｜交叉→heatmap｜机会→scatter｜5维→radar｜
进度→gauge｜事件→timeline｜热度→tag_cloud
"""

from collections.abc import Sequence

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

def forecast_series(values: Sequence[float], periods: int = 7) -> tuple[list[float], list[float]]:
    """极简趋势外推（最小二乘线性）+ 波动带宽（不含 ML 承诺）

    返回 (预测值, 带宽半宽)。用途：与真实值对比，判断动作是否改变了轨迹。
    """
    n = len(values)
    if n < 3 or periods <= 0:
        return [], []
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(values) / n
    denom = sum((x - mean_x) ** 2 for x in xs) or 1.0
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, values, strict=False)) / denom
    intercept = mean_y - slope * mean_x
    resid = [values[i] - (intercept + slope * xs[i]) for i in range(n)]
    std = (sum(r * r for r in resid) / n) ** 0.5
    preds = [intercept + slope * (n + k) for k in range(periods)]
    band = [1.96 * std] * periods
    return [max(0.0, p) for p in preds], band


def forecast_series_seasonal(values: Sequence[float], periods: int = 7,
                            season: int | None = None
                            ) -> tuple[list[float], list[float]]:
    """季节性外推：季节指数分解 + 去季节化线性趋势 + 复原

    比纯线性外推更贴近日报/周报的周期规律。样本不足自动回落线性外推。
    仍然不是 ML/ARIMA（诚实标注：`seasonal-naive + trend`）。
    """
    n = len(values)
    if periods <= 0:
        return [], []
    if n < 8:
        return forecast_series(values, periods)
    if not season:
        # 自动探测：日粒度数据常见 7 天周期；样本够长则回落到 7
        season = 7 if n >= 14 else n
    season = max(2, min(season, n // 2))
    buckets: list[list[float]] = [[] for _ in range(season)]
    for i, v in enumerate(values):
        buckets[i % season].append(float(v))
    overall = sum(float(v) for v in values) / n
    idx = []
    for b in buckets:
        m = sum(b) / len(b) if b else overall
        idx.append((m / overall) if overall else 1.0)
    des = [(float(v) / idx[i % season]) if idx[i % season] else float(v)
           for i, v in enumerate(values)]
    preds, band = forecast_series(des, periods)
    if not preds:
        return [], []
    out = [max(0.0, p * idx[(n + k) % season]) for k, p in enumerate(preds)]
    return out, band


def anomaly_points(values: Sequence[float], *, k: float = 2.5,
                   window: int = 7) -> tuple[list[float], list[float], list[int]]:
    """滚动均值 ± kσ 异常带（统计口径明确，非 ML）

    返回 (上界, 下界, 异常点下标)。样本 < window 时用全局均值/标准差。
    """
    vals = [float(v) for v in values]
    n = len(vals)
    if n < 3:
        return [], [], []
    win = max(3, min(window, n))
    upper, lower, bad = [], [], []
    for i in range(n):
        # 留一法：统计量剔除当前点，避免异常点把自身上下限撑大（自掩蔽）
        lo = max(0, i - win)
        seg = vals[lo:i]
        if len(seg) < 3:
            seg = vals[:i] + vals[i + 1:]
        if not seg:
            upper.append(vals[i])
            lower.append(vals[i])
            continue
        mean = sum(seg) / len(seg)
        var = sum((x - mean) ** 2 for x in seg) / len(seg)
        std = var ** 0.5
        upper.append(max(0.0, mean + k * std))
        lower.append(max(0.0, mean - k * std))
        dev = abs(vals[i] - mean)
        if std > 0:
            if dev > k * std:
                bad.append(i)
        elif dev > max(1e-9, abs(mean) * 0.25):
            bad.append(i)   # 平稳序列上的突变（σ=0）同样视为异常
    return upper, lower, bad


def anomaly_points_robust(values: Sequence[float], *, k: float = 3.5,
                          window: int = 14) -> tuple[list[float], list[float], list[int]]:
    """稳健异常检测：滚动中位数 + MAD（1.4826 缩放），比均值±σ 抗离群

    MAD ≈ 0（平稳段）时退化为「与中位数偏离超过 25% 即异常」。
    """
    vals = [float(v) for v in values]
    n = len(vals)
    if n < 4:
        return [], [], []
    win = max(4, min(window, n))
    upper, lower, bad = [], [], []
    for i in range(n):
        seg = vals[max(0, i - win):i]          # 留一：不含当前点
        if len(seg) < 3:
            seg = vals[:i] + vals[i + 1:]
        if not seg:
            continue
        srt = sorted(seg)
        med = srt[len(srt) // 2] if len(srt) % 2 else (srt[len(srt) // 2 - 1] +
                                                       srt[len(srt) // 2]) / 2
        devs = sorted(abs(x - med) for x in seg)
        mad = devs[len(devs) // 2]
        scale = 1.4826 * mad
        upper.append(max(0.0, med + k * scale))
        lower.append(max(0.0, med - k * scale))
        if scale > 0:
            if abs(vals[i] - med) > k * scale:
                bad.append(i)
        elif abs(vals[i] - med) > max(1e-9, abs(med) * 0.25):
            bad.append(i)
    return upper, lower, bad


def anomaly_points_seasonal(values: Sequence[float], *, k: float = 3.5,
                            season: int = 7) -> tuple[list[float], list[float], list[int]]:
    """季节残差异常：先按季节位置（如星期几）去季节，再对残差做稳健检测"""
    vals = [float(v) for v in values]
    n = len(vals)
    if n < 2 * season:
        return anomaly_points_robust(vals, k=k)
    buckets: list[list[float]] = [[] for _ in range(season)]
    for i, v in enumerate(vals):
        buckets[i % season].append(v)
    idx = [sorted(b)[len(b) // 2] if b else 0.0 for b in buckets]
    resid = [vals[i] - idx[i % season] for i in range(n)]
    up, low, bad = anomaly_points_robust(resid, k=k, window=max(7, season * 2))
    # 残差带 → 还原到原量纲（用于画带）
    return ([u + idx[i % season] for i, u in enumerate(up)],
            [max(0.0, lo + idx[i % season]) for i, lo in enumerate(low)], bad)


def line_chart(series: list[dict], labels: Sequence[str], *, width: int = 720,
               height: int = 220, y_label: str = "", as_area: bool = False,
               compare: dict | None = None, annotations: Sequence[dict] | None = None,
               forecast_periods: int = 0, canvas_threshold: int = 400,
               chart_id: str = "", anomaly: bool = False, anomaly_k: float = 2.5,
               anomaly_method: str = "sigma", seasonal: bool = False,
               season: int | None = None, anim: bool = True) -> str:
    """多序列趋势图

    compare: {"name": "上期", "series": [{"name","values","color"?}]} —— 虚线对比（同环比）
    annotations: [{"ts"/"pos", "label", "kind": "insight|action|verified", "severity"}] —— 钉在时间轴
    forecast_periods: >0 时按趋势外推（虚线 + 置信带；seasonal=True 用季节分解）
    canvas_threshold: 数据点超过该值改用 Canvas 渲染（大数据量），由前端 JS 绘制
    anomaly: 叠加滚动均值 ± kσ 异常带并标记越界点
    anim: 首次渲染做淡入/描线动画（前端尊重 prefers-reduced-motion）
    """
    if not series or not any(s.get("values") for s in series):
        return placeholder(width, height)

    pad_l, pad_r, pad_t, pad_b = 44, 12, 14, 26
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b

    # 预算外推，决定坐标上界
    forecast = {}
    if forecast_periods:
        for s in series:
            if seasonal:
                preds, band = forecast_series_seasonal(list(s["values"]),
                                                       forecast_periods, season)
            else:
                preds, band = forecast_series(list(s["values"]), forecast_periods)
            if preds:
                forecast[s["name"]] = {"preds": preds, "band": band}

    # 异常带：sigma（均值±kσ）/ mad（中位±k·MAD，抗离群）/ seasonal（去季节残差）
    anomaly_band = {}
    if anomaly:
        for s in series:
            vals = list(s["values"])
            if anomaly_method == "mad":
                up, low, bad = anomaly_points_robust(vals, k=anomaly_k or 3.5)
            elif anomaly_method == "seasonal":
                up, low, bad = anomaly_points_seasonal(
                    vals, k=anomaly_k or 3.5, season=season or 7)
            else:
                up, low, bad = anomaly_points(vals, k=anomaly_k)
            if up:
                anomaly_band[s["name"]] = {"upper": up, "lower": low, "points": bad,
                                           "method": anomaly_method}
    y_candidates = [max(s["values"]) if s["values"] else 0 for s in series]
    for f in forecast.values():
        y_candidates.append(max(f["preds"]))
    if compare:
        for s in compare.get("series", []):
            if s.get("values"):
                y_candidates.append(max(s["values"]))
    y_max = nice_max(max(y_candidates) if y_candidates else 1)

    total_points = sum(len(s.get("values") or []) for s in series)
    import json as _json
    meta = {
        "labels": list(labels)[:2000],
        "series": [{"name": s["name"], "values": list(s["values"])[:2000],
                    "color": s.get("color") or theme.series_color(i)}
                   for i, s in enumerate(series)],
        "plot": [pad_l, pad_t, plot_w, plot_h], "y_max": y_max, "as_area": as_area,
        "compare": None, "forecast": {k: {"preds": v["preds"], "band": v["band"]}
                                      for k, v in forecast.items()},
        "annotations": [],
        "anomaly": anomaly_band, "anomaly_method": anomaly_method,
        "anim": bool(anim),
        "kind": "line",
    }
    if compare:
        meta["compare"] = {
            "name": compare.get("name", "上期"),
            "series": [{"name": s["name"], "values": list(s["values"])[:2000],
                        "color": s.get("color") or theme.MUTED}
                       for s in compare.get("series", [])],
        }

    # 注释：把洞察/动作钉到时间轴上（视觉化"洞察 → 动作 → 验证"）
    ann_html = []
    if annotations:
        total_slots = max(1, len(labels) - 1)
        for ann in annotations:
            pos = ann.get("pos")
            if pos is None and ann.get("ts") and labels:
                idx = _nearest_label_index(str(ann["ts"]), list(labels))
                if idx is None:
                    continue
                pos = idx
            if pos is None:
                continue
            x = pad_l + plot_w * min(1.0, max(0.0, float(pos) / total_slots))
            color = theme.severity_color(str(ann.get("kind", ann.get("severity", "info"))))
            label = str(ann.get("label", ""))
            meta["annotations"].append({"x": round(x, 1), "label": label,
                                        "kind": str(ann.get("kind", "insight"))})
            ann_html.append(
                f'<line x1="{x:.1f}" y1="{pad_t}" x2="{x:.1f}" y2="{pad_t + plot_h}" '
                f'stroke="{color}" stroke-width="1.2" stroke-dasharray="4 3" opacity="0.75"/>')
            ann_html.append(f'<circle cx="{x:.1f}" cy="{pad_t + 4}" r="3.2" fill="{color}"/>')
            if label:
                ann_html.append(
                    f'<text x="{x + 4:.1f}" y="{pad_t + 12}" class="ann" '
                    f'style="font-size:9.5px;fill:{color}">{esc(label[:14])}</text>')

    # 大数据量 → Canvas（前端绘制，避免 SVG 节点爆炸）
    if total_points > canvas_threshold:
        meta_json = _json.dumps(meta, ensure_ascii=False)
        cid = chart_id or f"c{abs(hash(meta_json)) % 1000000:06d}"
        return (f'<div class="if-chart" data-canvas="1" id="{cid}">'
                f'<canvas width="{width}" height="{height}" '
                f'data-chart=\'{esc(meta_json)}\' '
                f'role="img" aria-label="{esc(y_label or "趋势图")}（{total_points} 个数据点，Canvas 渲染）" '
                f'style="width:100%;height:{height}px"></canvas></div>')

    body = [y_grid(y_max, pad_l, pad_t, plot_w, plot_h)]
    # 对比（上期）虚线
    if compare:
        for s in compare.get("series", []):
            if not s.get("values"):
                continue
            color = s.get("color") or theme.MUTED
            pts, _ = line_points(list(s["values"]), pad_l, pad_t, plot_w, plot_h, y_max)
            nm = str(compare.get("name", "上期"))
            body.append(f'<polyline points="{pts}" fill="none" stroke="{color}" '
                        f'stroke-width="1.4" stroke-dasharray="5 4" opacity="0.75" '
                        f'data-series="{esc(nm)}"/>')
    legend_items = []
    for i, s in enumerate(series):
        color = s.get("color") or theme.series_color(i)
        legend_items.append((s["name"], color))
        pts, coords = line_points(list(s["values"]), pad_l, pad_t, plot_w, plot_h, y_max)
        snm = esc(str(s["name"]))
        if as_area and coords:
            body.append(f'<polygon points="{coords[0][0]:.1f},{pad_t + plot_h} {pts} '
                        f'{coords[-1][0]:.1f},{pad_t + plot_h}" fill="{color}" opacity="0.10" '
                        f'data-series="{snm}"/>')
        body.append(f'<polyline points="{pts}" fill="none" stroke="{color}" '
                    f'stroke-width="2" stroke-linejoin="round" data-series="{snm}"/>')
        for idx, (x, y) in enumerate(coords):
            label = labels[idx] if idx < len(labels) else ""
            tip = f"{label} · {s['name']}: {fmt_num(s['values'][idx])}"
            body.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{2.2 if len(coords) <= 40 else 0.1}" '
                        f'fill="{color}" data-tip="{esc(tip)}" class="pt" tabindex="0" '
                        f'data-series="{snm}" aria-label="{esc(tip)}"/>')
        # 外推虚线 + 置信带
        f = forecast.get(s["name"])
        if f and f["preds"]:
            xs = [pad_l + plot_w * (len(coords) - 1 + k + 1) / max(1, len(labels) - 1 + len(f["preds"]))
                  for k in range(len(f["preds"]))]
            pts = []
            for x, v in zip(xs, f["preds"], strict=False):
                y = pad_t + plot_h - (max(0.0, v) / y_max) * plot_h
                pts.append(f"{x:.1f},{y:.1f}")
            up = [f"{x:.1f},{pad_t + plot_h - (min(y_max, v + b) / y_max) * plot_h:.1f}"
                  for x, v, b in zip(xs, f["preds"], f["band"], strict=False)]
            down = [f"{x:.1f},{pad_t + plot_h - (max(0.0, v - b) / y_max) * plot_h:.1f}"
                    for x, v, b in zip(xs, f["preds"], f["band"], strict=False)]
            band_pts = " ".join(up + list(reversed(down)))
            body.append(f'<polygon points="{band_pts}" fill="{color}" opacity="0.10"/>')
            body.append(f'<polyline points="{" ".join(pts)}" fill="none" stroke="{color}" '
                        f'stroke-width="1.6" stroke-dasharray="6 4" opacity="0.85"/>')
            if pts:
                body.append(f'<text x="{xs[0]:.1f}" y="{pad_t + 10}" '
                            f'style="font-size:9px;fill:{color}">预测</text>')
    # 异常带（滚动 ± kσ）+ 越界点标注
    if anomaly_band:
        for sname, ab in anomaly_band.items():
            col = theme.DANGER
            up_pts = [f"{pad_l + plot_w * i / max(1, len(labels) - 1):.1f},"
                      f"{pad_t + plot_h - (min(y_max, v) / y_max) * plot_h:.1f}"
                      for i, v in enumerate(ab["upper"][:len(labels)])]
            dn_pts = [f"{pad_l + plot_w * i / max(1, len(labels) - 1):.1f},"
                      f"{pad_t + plot_h - (max(0.0, v) / y_max) * plot_h:.1f}"
                      for i, v in enumerate(ab["lower"][:len(labels)])]
            if up_pts:
                body.append(f'<polygon points="{" ".join(up_pts + list(reversed(dn_pts)))}" '
                            f'fill="{col}" opacity="0.07" data-series="__anomaly__"/>')
            for i in ab["points"]:
                if i >= len(labels):
                    continue
                vals = None
                for s2 in series:
                    if str(s2["name"]) == sname:
                        vals = list(s2["values"])
                if not vals or i >= len(vals):
                    continue
                x = pad_l + plot_w * i / max(1, len(labels) - 1)
                y = pad_t + plot_h - (min(y_max, max(0.0, vals[i])) / y_max) * plot_h
                body.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.2" fill="none" '
                            f'stroke="{col}" stroke-width="2" class="anom" '
                            f'data-tip="{esc(str(labels[i]))} · 异常值 {fmt_num(vals[i])}" '
                            f'tabindex="0" aria-label="{esc(str(labels[i]))} 异常值 {fmt_num(vals[i])}"/>')
    body.extend(ann_html)
    body.append(axis_labels(labels, pad_l, pad_t + plot_h, plot_w))
    if len(series) > 1 or compare:
        items = list(legend_items)
        if compare and compare.get("series"):
            items.append((compare.get("name", "上期"), theme.MUTED))
        body.append(legend(items, pad_l, 10))
    if y_label:
        body.append(f'<text x="{pad_l}" y="{pad_t - 4}" style="font-size:10px;'
                    f'fill:{theme.FAINT}">{esc(y_label)}</text>')

    meta["series"] = [{"name": s["name"], "values": s["values"], "color": s["color"]}
                      for s in meta["series"]]
    meta_json = _json.dumps(meta, ensure_ascii=False)
    aria = (f"{y_label or '趋势图'}：{'、'.join(s['name'] for s in series)}；"
            f"{len(labels)} 个时间点"
            + (f"；含{len(annotations)}个标注" if annotations else "")
            + (f"；含{forecast_periods}期预测" if forecast_periods else "")
            + (f"；异常标记（{anomaly_method}）" if anomaly_band else ""))
    cls = "chart if-anim" if anim else "chart"
    return svg(width, height, "".join(body), cls=cls).replace(
        "<svg ", f'<svg data-chart=\'{esc(meta_json)}\' role="img" aria-label="{esc(aria)}" ', 1)


def _nearest_label_index(ts: str, labels: Sequence[str]) -> int | None:
    """时间字符串 → 最近的时间轴下标（注释定位用；支持前缀匹配）"""
    key = ts[:10]
    for i, lab in enumerate(labels):
        if str(lab)[:10] >= key:
            return i
    return len(labels) - 1 if labels else None



# ========== 横向条形（分类对比）==========

def bar_chart(items: Sequence[tuple[str, float]], *, width: int = 720,
              height: int | None = None, color: str = theme.ACCENT,
              threshold: float | None = None, threshold_label: str = "",
              value_fmt=fmt_num, colors: Sequence[str] | None = None,
              filter_dim: str = "") -> str:
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
        cf = (f' data-cf="{esc(filter_dim)}:{esc(label)}" data-cf-label="{esc(label)}"'
              if filter_dim else "")
        body.append(f'<rect x="{label_w}" y="{y + 3}" width="{w:.1f}" height="12" rx="6" '
                    f'fill="{c}"{cf} '
                    f'data-tip="{esc(label)}: {esc(value_fmt(value))}" '
                    f'data-drill-entity="{esc(label)}" class="bar"/>')
        body.append(f'<text x="{label_w + bar_w + 8}" y="{y + 13}" '
                    f'style="font-size:11px;fill:{theme.MUTED};font-weight:600">'
                    f'{esc(value_fmt(value))}</text>')
    aria = "横向条形图：" + "；".join(f"{k} {value_fmt(v)}" for k, v in items[:6])
    if threshold is not None and v_max:
        tx = label_w + bar_w * threshold / v_max
        body.append(f'<line x1="{tx:.1f}" y1="6" x2="{tx:.1f}" y2="{height - 6}" '
                    f'stroke="{theme.DANGER}" stroke-width="1.4" stroke-dasharray="4 3"/>')
        if threshold_label:
            body.append(f'<text x="{tx + 4:.1f}" y="12" style="font-size:9.5px;'
                        f'fill:{theme.DANGER}">{esc(threshold_label)}</text>')
    return svg(width, height, "".join(body), aria=aria)


# ========== 构成（堆叠条 / 百分比条）==========

def percent_bar(parts: Sequence[tuple[str, float, str]], *, width: int = 720,
                height: int = 26, show_legend: bool = True,
                filter_dim: str = "") -> str:
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
        cf = (f' data-cf="{esc(filter_dim)}:{esc(str(name))}" data-cf-label="{esc(str(name))}"'
              if filter_dim else "")
        body.append(f'<rect x="{x:.1f}" y="0" width="{max(0.5, w):.1f}" height="{height}" '
                    f'fill="{color}"{cf} '
                    f'data-tip="{esc(name)}: {fmt_num(value)}（{pct:.0%}）" class="seg"/>')
        if w > 34:  # 太窄不放字
            body.append(f'<text x="{x + w / 2:.1f}" y="{height / 2 + 4:.1f}" '
                        f'text-anchor="middle" style="font-size:10.5px;fill:'
                        f'{theme.SURFACE if False else "oklch(100% 0 0)"}">{pct:.0%}</text>')
        x += w
    aria = "构成占比：" + "；".join(
        f"{n} {fmt_num(v)}（{fmt_pct(float(v) / total)}）" for n, v, _ in parts[:6])
    out = svg(width, height, "".join(body), aria=aria)
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
        for nm, value, color in parts:
            h = plot_h * max(0.0, float(value)) / y_max
            if h <= 0:
                continue
            y_cursor -= h
            body.append(f'<rect x="{x:.1f}" y="{y_cursor:.1f}" width="{bar_w:.1f}" '
                        f'height="{h:.1f}" fill="{color}" class="seg" '
                        f'data-tip="{esc(nm)}: {fmt_num(value)}"/>')
    body.append(axis_labels([r[0] for r in rows], pad_l, pad_t + plot_h, plot_w))
    aria = "堆叠柱状图：" + "；".join(str(r[0]) for r in rows[:6])
    names = [nm for nm, _, _ in rows[0][1]] if rows and rows[0][1] else []
    if names:
        colors = [c for _, _, c in rows[0][1]]
        body.append(legend(list(zip(names, colors, strict=False)), pad_l, 10))
    return svg(width, height, "".join(body), aria=aria)


# ========== 漏斗（旅程 / 转化）==========

def funnel(steps: Sequence[tuple[str, float]], *, width: int = 720,
           height: int = 240) -> str:
    """漏斗图（相邻步转化率标注）"""
    steps = [(str(k), float(v or 0)) for k, v in steps]
    if not steps:
        return placeholder(width, height)
    top = steps[0][1] or 1
    n = len(steps)
    band_h = max(24, (height - 10) / n - 6)
    aria = "漏斗图：" + "；".join(f"{k} {fmt_num(v)}" for k, v in steps[:6])
    body = []
    for i, (label, value) in enumerate(steps):
        ratio = value / top
        w = max(6.0, (width - 200) * ratio)
        x = (width - 200 - w) / 2 + 8
        y = 6 + i * (band_h + 6)
        color = theme.series_color(i)
        body.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{band_h:.1f}" '
                    f'rx="6" fill="{color}" opacity="0.86" class="seg" '
                    f'data-tip="{esc(label)}: {fmt_num(value)}（占首步 {ratio:.0%}）"/>')
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
    return svg(width, height, "".join(body), aria=aria)


# ========== 热力矩阵（双维交叉）==========

def heatmap(rows: Sequence[str], cols: Sequence[str], matrix: Sequence[Sequence[float]], *,
            width: int = 720, height: int | None = None,
            row_w: int = 120, canvas_threshold: int = 900,
            chart_id: str = "") -> str:
    """热力矩阵（如旅程阶段 × 触点数、时段 × 渠道）"""
    if not rows or not cols:
        return placeholder(width, height or 160)
    cell_w = max(18, (width - row_w - 8) / len(cols))
    cell_h = 26
    height = height or max(80, 20 + cell_h * len(rows))
    if len(rows) * len(cols) > canvas_threshold:
        import json as _json
        meta = {"kind": "heatmap", "rows": [str(r) for r in rows],
                "cols": [str(c) for c in cols],
                "matrix": [[float(matrix[i][j]) if i < len(matrix) and j < len(matrix[i])
                            else 0.0 for j in range(len(cols))] for i in range(len(rows))],
                "plot": [row_w + 1, 18, cell_w, cell_h], "anim": True}
        cid = chart_id or f"hm{abs(hash(_json.dumps(meta, ensure_ascii=False))) % 1000000:06d}"
        return (f'<div class="if-chart" data-canvas="1" id="{cid}">'
                f'<canvas width="{width}" height="{height}" '
                f"data-chart='{esc(_json.dumps(meta, ensure_ascii=False))}' "
                f'role="img" aria-label="热力矩阵：{len(rows)} 行 × {len(cols)} 列（Canvas 渲染）" '
                f'style="width:100%;height:{height}px"></canvas></div>')
    aria = f"热力矩阵：{len(rows)} 行 × {len(cols)} 列"
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
                        f'fill="{theme.ACCENT}" opacity="{opacity:.2f}" class="cell" '
                        f'data-tip="{esc(str(r))} × {esc(str(c))}: {v:.2f}"/>')
    return svg(width, height, "".join(body), aria=aria)


# ========== 散点四象限（机会矩阵）==========

def scatter(points: Sequence[tuple[float, float, str]], *, x_label: str = "",
            y_label: str = "", width: int = 720, height: int = 300,
            x_mid: float | None = None, y_mid: float | None = None,
            good_quadrant: str = "tr", canvas_threshold: int = 800,
            chart_id: str = "", max_points: int = 20000) -> str:
    """散点（如关键词机会：搜索量 × 排名/竞争力），支持四象限标注"""
    pts = [(float(x or 0), float(y or 0), str(lbl)) for x, y, lbl in points]
    if not pts:
        return placeholder(width, height)
    pad_l, pad_r, pad_t, pad_b = 46, 14, 16, 28
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    x_max = nice_max(max(p[0] for p in pts) or 1)
    y_max = nice_max(max(p[1] for p in pts) or 1)
    xm = x_mid if x_mid is not None else x_max / 2
    ym = y_mid if y_mid is not None else y_max / 2
    # 大数据量 → Canvas/WebGL（散点 > 800 个时 SVG 节点会明显拖慢）
    if len(pts) > canvas_threshold:
        import json as _json
        total_pts = len(pts)
        sampled = False
        if total_pts > max_points:          # 等步长抽样：保留分布形态，控制传输体积
            stride = (total_pts + max_points - 1) // max_points
            pts = pts[::stride]
            sampled = True
        meta = {"kind": "scatter",
                "points": [[round(x, 4), round(y, 4), lbl] for x, y, lbl in pts],
                "sampled": sampled, "sampled_from": total_pts,
                "x_max": x_max, "y_max": y_max, "x_mid": xm, "y_mid": ym,
                "x_label": x_label, "y_label": y_label, "plot": [pad_l, pad_t, plot_w, plot_h],
                "good_quadrant": good_quadrant, "anim": True}
        cid = chart_id or f"sc{abs(hash(_json.dumps(meta, ensure_ascii=False))) % 1000000:06d}"
        return (f'<div class="if-chart" data-canvas="1" id="{cid}">'
                f'<canvas width="{width}" height="{height}" '
                f"data-chart='{esc(_json.dumps(meta, ensure_ascii=False))}' "
                f'role="img" aria-label="{esc(x_label or "散点")} × {esc(y_label or "指标")}'
                f'：{total_pts} 个点（GPU/Canvas 渲染'
                + (f"，等步长抽样至 {len(pts)} 点" if sampled else "") + '）" '
                f'style="width:100%;height:{height}px"></canvas></div>')
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
        body.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4.5" fill="{color}" opacity="0.85" '
                    f'class="pt" data-drill-entity="{esc(label)}" '
                    f'data-tip="{esc(label)} · {esc(x_label)}={fmt_num(x)} · {esc(y_label)}={fmt_num(y)}">'
                    f'<title>{esc(label)}（{fmt_num(x)}, {fmt_num(y)}）</title></circle>')
    if x_label:
        body.append(f'<text x="{pad_l + plot_w / 2:.1f}" y="{height - 4}" text-anchor="middle" '
                    f'style="font-size:10.5px;fill:{theme.MUTED}">{esc(x_label)}</text>')
    if y_label:
        body.append(f'<text x="{pad_l}" y="{pad_t - 5}" style="font-size:10.5px;'
                    f'fill:{theme.MUTED}">{esc(y_label)}</text>')
    aria = f"散点图（{x_label} × {y_label}）：{len(pts)} 个点"
    return svg(width, height, "".join(body), aria=aria)


# ========== 雷达（多维评分）==========

def radar(axes: Sequence[tuple[str, float]], *, width: int = 320, height: int = 260,
          color: str = theme.ACCENT) -> str:
    """雷达图（axes: [(label, 0..1)]）——成熟度五维"""
    axes = [(str(k), max(0.0, min(1.0, float(v)))) for k, v in axes]
    if not axes:
        return placeholder(width, height)
    import math
    cx, cy, R = width / 2, height / 2 + 6, min(width, height) / 2 - 40  # noqa: N806
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
    aria = "雷达图：" + "；".join(f"{k} {fmt_pct(v)}" for k, v in axes)
    return svg(width, height, "".join(body), aria=aria)


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
    return svg(size, size + 10, "".join(body),
               aria=f"{label or '进度'}：{fmt_pct(v)}")


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

# ========== 桑基（流量/来源→转化路径）==========

def sankey(flows: Sequence[tuple[str, str, float]], *, width: int = 720,
           height: int = 300, unit: str = "") -> str:
    """桑基图：flows=[(来源, 去向, 数值)]

    分层规则：来源按出现顺序排左列，去向排右列；跨层流量用贝塞尔带连接。
    纯 SVG 零依赖，节点带宽即流量占比。
    """
    items = [(str(a), str(b), float(v or 0)) for a, b, v in flows if float(v or 0) > 0]
    if not items:
        return placeholder(width, height)
    srcs: list[str] = []
    dsts: list[str] = []
    for a, b, _ in items:
        if a not in srcs:
            srcs.append(a)
        if b not in dsts:
            dsts.append(b)
    total = sum(v for _, _, v in items)
    out_sum = {k: sum(v for a, _, v in items if a == k) for k in srcs}
    in_sum = {k: sum(v for _, b, v in items if b == k) for k in dsts}
    pad_t, pad_b, node_w = 12, 24, 14
    plot_h = height - pad_t - pad_b
    gap = 6 if len(srcs) <= 6 else 3
    sx, dx = 8, width - 8 - node_w

    def stack(names: list[str], sums: dict, x: float):
        """按流量比例分配节点 y 位置与高度"""
        avail = plot_h - gap * max(0, len(names) - 1)
        pos, y = {}, pad_t
        for k in names:
            h = max(4.0, (sums[k] / total) * avail)
            pos[k] = [y, h]
            y += h + gap
        return pos

    spos, dpos = stack(srcs, out_sum, sx), stack(dsts, in_sum, dx)
    s_cursor = {k: v[0] for k, v in spos.items()}
    d_cursor = {k: v[0] for k, v in dpos.items()}
    body = []
    for _i, (a, b, v) in enumerate(sorted(items, key=lambda t: (srcs.index(t[0]),
                                                              dsts.index(t[1])))):
        sh = max(2.0, (v / total) * (plot_h - gap * max(0, len(srcs) - 1)))
        dh = max(2.0, (v / total) * (plot_h - gap * max(0, len(dsts) - 1)))
        y0 = s_cursor[a]
        s_cursor[a] = y0 + sh
        y1 = d_cursor[b]
        d_cursor[b] = y1 + dh
        color = theme.series_color(srcs.index(a))
        c1 = sx + node_w + (dx - sx - node_w) * 0.42
        c2 = sx + node_w + (dx - sx - node_w) * 0.58
        body.append(
            f'<path d="M{sx + node_w:.1f},{y0:.1f} C{c1:.1f},{y0:.1f} {c2:.1f},{y1:.1f} '
            f'{dx:.1f},{y1:.1f} L{dx:.1f},{y1 + dh:.1f} C{c2:.1f},{y1 + dh:.1f} '
            f'{c1:.1f},{y0 + sh:.1f} {sx + node_w:.1f},{y0 + sh:.1f} Z" '
            f'fill="{color}" opacity="0.32" class="flow" '
            f'data-tip="{esc(a)} → {esc(b)}：{fmt_num(v)}{esc(unit)}"/>')
    for k in srcs:
        y, h = spos[k]
        body.append(f'<rect x="{sx}" y="{y:.1f}" width="{node_w}" height="{h:.1f}" '
                    f'rx="3" fill="{theme.series_color(srcs.index(k))}" '
                    f'data-tip="{esc(k)}：{fmt_num(out_sum[k])}{esc(unit)}"/>')
        body.append(f'<text x="{sx + node_w + 5}" y="{y + h / 2 + 3:.1f}" '
                    f'style="font-size:10.5px;fill:{theme.FG}">{esc(k[:14])} '
                    f'<tspan style="fill:{theme.FAINT}">{fmt_num(out_sum[k])}</tspan></text>')
    for k in dsts:
        y, h = dpos[k]
        body.append(f'<rect x="{dx}" y="{y:.1f}" width="{node_w}" height="{h:.1f}" '
                    f'rx="3" fill="{theme.MUTED}" '
                    f'data-tip="{esc(k)}：{fmt_num(in_sum[k])}{esc(unit)}"/>')
        body.append(f'<text x="{dx - 5}" y="{y + h / 2 + 3:.1f}" text-anchor="end" '
                    f'style="font-size:10.5px;fill:{theme.FG}">{esc(k[:14])} '
                    f'<tspan style="fill:{theme.FAINT}">{fmt_num(in_sum[k])}</tspan></text>')
    aria = (f"桑基图：{len(srcs)} 个来源 → {len(dsts)} 个去向，合计 {fmt_num(total)}"
            + (f" {unit}" if unit else ""))
    return svg(width, height, "".join(body), aria=aria)


# ========== 网格地图（tile grid map，非精确边界）==========

# 中国省级网格排布（7 列 × 8 行，近似地理相对位置；用于快速地理分布判断）
CHINA_GRID = {
    "黑龙江": (6, 0), "吉林": (6, 1), "辽宁": (5, 2), "内蒙古": (4, 1),
    "北京": (4.6, 2), "天津": (5.1, 2.4), "河北": (4.5, 2.6), "山西": (3.9, 3),
    "陕西": (3.4, 3.6), "宁夏": (2.9, 3.2), "甘肃": (2.4, 3.4), "青海": (1.6, 3.6),
    "新疆": (0.9, 2.6), "西藏": (1.1, 4.6), "四川": (2.7, 4.4), "重庆": (3.3, 4.5),
    "云南": (2.6, 5.4), "贵州": (3.2, 5.1), "广西": (3.6, 5.8), "海南": (4.2, 6.6),
    "广东": (4.5, 5.7), "湖南": (4.0, 4.9), "湖北": (4.3, 4.3), "河南": (4.4, 3.5),
    "山东": (5.0, 2.9), "江苏": (5.3, 3.5), "安徽": (4.9, 4.0), "上海": (5.9, 3.9),
    "浙江": (5.6, 4.4), "江西": (4.8, 4.9), "福建": (5.3, 5.2), "台湾": (6.1, 5.2),
    "香港": (4.9, 6.1), "澳门": (4.5, 6.2),
}

# 全球主要市场网格（8 列 × 5 行，近似相对位置）
WORLD_GRID = {
    "美国": (1, 1), "加拿大": (1.4, 0.4), "墨西哥": (1.3, 1.8), "巴西": (2.6, 2.8),
    "阿根廷": (2.4, 3.6), "智利": (2.1, 3.4), "英国": (3.9, 0.8), "爱尔兰": (3.7, 0.8),
    "法国": (4.1, 1.2), "德国": (4.3, 1.1), "荷兰": (4.2, 0.9), "西班牙": (3.9, 1.6),
    "意大利": (4.4, 1.4), "瑞士": (4.2, 1.3), "瑞典": (4.4, 0.6), "挪威": (4.2, 0.4),
    "波兰": (4.6, 1.1), "俄罗斯": (5.6, 0.7), "土耳其": (4.9, 1.6), "沙特": (5.1, 2.1),
    "阿联酋": (5.4, 2.1), "印度": (5.8, 2.2), "巴基斯坦": (5.6, 2.0), "泰国": (6.2, 2.6),
    "越南": (6.3, 2.4), "马来西亚": (6.2, 2.9), "新加坡": (6.2, 3.1), "印尼": (6.4, 3.2),
    "菲律宾": (6.6, 2.6), "中国": (6.0, 1.7), "日本": (6.8, 1.4), "韩国": (6.6, 1.6),
    "台湾": (6.6, 1.9), "香港": (6.1, 2.2), "澳大利亚": (6.7, 3.8), "新西兰": (7.1, 4.2),
    "南非": (4.6, 3.6), "埃及": (4.7, 2.2), "尼日利亚": (4.0, 2.6), "肯尼亚": (5.0, 2.9),
}


def tile_map(items: Sequence[tuple[str, float]], *, width: int = 720, height: int = 320,
             scope: str = "china", unit: str = "", label: str = "",
             filter_dim: str = "province") -> str:
    """网格地图（tile grid map）：地理相对位置 + 指标热力

    诚实标注：非精确边界/非投影地图，用于「哪个区域多/少」的快速判断；
    需要精确地图时接入 GeoJSON（当前零依赖约束下不内置）。
    """
    grid = CHINA_GRID if scope == "china" else WORLD_GRID
    data = {str(k): float(v or 0) for k, v in items}
    cols = max(x for x, _ in grid.values()) + 1
    rows = max(y for _, y in grid.values()) + 1
    cell = min((width - 24) / cols, (height - 30) / rows)
    vmax = max(data.values(), default=0) or 1
    body = []
    for name, (gx, gy) in grid.items():
        v = data.get(name)
        x, y = 12 + gx * cell, 18 + gy * cell
        if v is None:
            fill, op, col = theme.BORDER, 0.35, theme.FAINT
        else:
            fill, op, col = theme.ACCENT, 0.12 + 0.88 * (v / vmax), theme.FG
        cf = (f' data-cf="{esc(filter_dim)}:{esc(name)}" data-cf-label="{esc(name)}"'
              if v is not None else "")
        body.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{cell - 2:.1f}" '
                    f'height="{cell - 2:.1f}" rx="4" fill="{fill}" opacity="{op:.2f}" '
                    f'class="cell"{cf} data-tip="{esc(name)}：'
                    f'{fmt_num(v) if v is not None else "无数据"}{esc(unit)}"/>')
        short = name[:3]
        body.append(f'<text x="{x + cell / 2 - 1:.1f}" y="{y + cell / 2 - 3:.1f}" '
                    f'text-anchor="middle" style="font-size:{max(8, cell * 0.26):.1f}px;'
                    f'fill:{col};pointer-events:none">{esc(short)}</text>')
        if v is not None:
            body.append(f'<text x="{x + cell / 2 - 1:.1f}" y="{y + cell / 2 + 9:.1f}" '
                        f'text-anchor="middle" style="font-size:{max(7.5, cell * 0.24):.1f}px;'
                        f'fill:{theme.MUTED};pointer-events:none">{fmt_num(v)}</text>')
    if label:
        body.append(f'<text x="12" y="11" style="font-size:10.5px;fill:{theme.MUTED}">'
                    f'{esc(label)}</text>')
    top = sorted(((k, v) for k, v in data.items()), key=lambda t: -t[1])[:5]
    aria = (f"网格地图（{'中国省份' if scope == 'china' else '全球市场'}）："
            + "；".join(f"{k} {fmt_num(v)}" for k, v in top))
    return svg(width, height, "".join(body), aria=aria)


# ========== 箱线图（分布对比）==========

def _quantile(sorted_vals: list[float], q: float) -> float:
    """线性插值分位数（与 numpy/pandas 默认一致）"""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def boxplot(groups: Sequence[tuple[str, Sequence[float]]], *, width: int = 720,
            height: int = 260, y_label: str = "") -> str:
    """箱线图：中位数/四分位/须（1.5IQR）/离群点；用于分布与稳定性对比"""
    data = [(str(k), sorted(float(x) for x in vals))
            for k, vals in groups if vals]
    if not data:
        return placeholder(width, height)
    pad_l, pad_r, pad_t, pad_b = 46, 12, 18, 30
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    y_max = nice_max(max(v[-1] for _, v in data) or 1)
    body = [y_grid(y_max, pad_l, pad_t, plot_w, plot_h)]
    step = plot_w / len(data)
    bw = min(46.0, step * 0.5)
    for i, (name, vals) in enumerate(data):
        q1, q2, q3 = (_quantile(vals, 0.25), _quantile(vals, 0.5), _quantile(vals, 0.75))
        iqr = q3 - q1
        lo_bound, hi_bound = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        inner = [v for v in vals if lo_bound <= v <= hi_bound] or vals
        w_lo, w_hi = inner[0], inner[-1]
        out = [v for v in vals if v < lo_bound or v > hi_bound]
        cx = pad_l + step * (i + 0.5)
        color = theme.series_color(i)

        def Y(v: float) -> float:
            return pad_t + plot_h - (max(0.0, min(y_max, v)) / y_max) * plot_h

        body.append(f'<line x1="{cx:.1f}" y1="{Y(w_hi):.1f}" x2="{cx:.1f}" y2="{Y(w_lo):.1f}" '
                    f'stroke="{color}" stroke-width="1.4"/>')
        body.append(f'<line x1="{cx - bw / 3:.1f}" y1="{Y(w_hi):.1f}" x2="{cx + bw / 3:.1f}" '
                    f'y2="{Y(w_hi):.1f}" stroke="{color}" stroke-width="1.4"/>')
        body.append(f'<line x1="{cx - bw / 3:.1f}" y1="{Y(w_lo):.1f}" x2="{cx + bw / 3:.1f}" '
                    f'y2="{Y(w_lo):.1f}" stroke="{color}" stroke-width="1.4"/>')
        body.append(f'<rect x="{cx - bw / 2:.1f}" y="{Y(q3):.1f}" width="{bw:.1f}" '
                    f'height="{max(1.0, Y(q1) - Y(q3)):.1f}" rx="3" fill="{color}" '
                    f'opacity="0.20" stroke="{color}" stroke-width="1.6" '
                    f'class="box" data-cf="channel:{esc(name)}" data-cf-label="{esc(name)}" '
                    f'data-tip="{esc(name)} · 中位 {fmt_num(q2)} · IQR '
                    f'{fmt_num(q1)}~{fmt_num(q3)} · n={len(vals)}"/>')
        body.append(f'<line x1="{cx - bw / 2:.1f}" y1="{Y(q2):.1f}" x2="{cx + bw / 2:.1f}" '
                    f'y2="{Y(q2):.1f}" stroke="{color}" stroke-width="2.4"/>')
        for v in out[:20]:
            body.append(f'<circle cx="{cx:.1f}" cy="{Y(v):.1f}" r="2.6" fill="none" '
                        f'stroke="{color}" stroke-width="1.4" '
                        f'data-tip="{esc(name)} · 离群 {fmt_num(v)}"/>')
        body.append(f'<text x="{cx:.1f}" y="{pad_t + plot_h + 16:.1f}" text-anchor="middle" '
                    f'style="font-size:10.5px;fill:{theme.MUTED}">{esc(name[:10])}</text>')
    if y_label:
        body.append(f'<text x="{pad_l}" y="{pad_t - 5}" style="font-size:10.5px;'
                    f'fill:{theme.MUTED}">{esc(y_label)}</text>')
    aria = ("箱线图：" + "；".join(
        f"{k} 中位 {fmt_num(_quantile(v, 0.5))}" for k, v in data[:6]))
    return svg(width, height, "".join(body), aria=aria)


# ========== 精确边界地图（GeoJSON choropleth）==========

def choropleth(dataset: dict, values: Sequence[tuple[str, float]], *, width: int = 720,
               height: int = 380, unit: str = "", label: str = "",
               filter_dim: str = "") -> str:
    """GeoJSON 精确边界地图 + 指标热力（数据由 engine/geo 提供并缓存）

    dataset: {"features": [{label, geometry, properties}], "name_key": "name"}
    values:  [(区域名, 值)]，与 feature.label 精确匹配（未匹配按"无数据"渲染）
    """
    from ..engine.geo import bounds_of, project
    feats = (dataset or {}).get("features") or []
    if not feats:
        return placeholder(width, height)
    data = {str(k): float(v or 0) for k, v in values}
    vmax = max(data.values(), default=0) or 1
    pad_l, pad_t = 6, 20
    plot_w, plot_h = width - 2 * pad_l, height - pad_t - 26
    bounds = bounds_of(feats)
    body = []
    matched = 0
    for f in feats:
        d = project(f["geometry"], bounds, plot_w, plot_h, pad=4)
        if not d:
            continue
        v = data.get(f["label"])
        if v is None:
            fill, op, tip = theme.BORDER, 0.35, f'{f["label"]}：无数据'
        else:
            matched += 1
            fill, op = theme.ACCENT, 0.12 + 0.88 * min(1.0, v / vmax)
            tip = f'{f["label"]}：{fmt_num(v)}{unit}'
        cf = (f' data-cf="{esc(filter_dim)}:{esc(f["label"])}" '
              f'data-cf-label="{esc(f["label"])}"') if (filter_dim and v is not None) else ""
        body.append(
            f'<g transform="translate({pad_l},{pad_t})">'
            f'<path d="{d}" fill="{fill}" opacity="{op:.2f}" stroke="{theme.SURFACE}" '
            f'stroke-width="0.6" class="geo" data-tip="{esc(tip)}"{cf}>'
            f'<title>{esc(tip)}</title></path></g>')
    if label:
        body.append(f'<text x="{pad_l}" y="12" style="font-size:10.5px;'
                    f'fill:{theme.MUTED}">{esc(label)}</text>')
    top = sorted(((k, v) for k, v in data.items()), key=lambda t: -t[1])[:5]
    aria = (f"边界地图（{len(feats)} 个区域，{matched} 个有数据）："
            + "；".join(f"{k} {fmt_num(v)}" for k, v in top))
    return svg(width, height, "".join(body), aria=aria)


# ========== 瀑布图（增减归因）==========

def waterfall(items: Sequence[tuple[str, float]], *, width: int = 720, height: int = 260,
              start: float = 0.0, unit: str = "", label: str = "") -> str:
    """瀑布图：把总变化拆成各因子贡献（正绿负红，末位为合计）"""
    rows = [(str(k), float(v or 0)) for k, v in items]
    if not rows:
        return placeholder(width, height)
    pad_l, pad_r, pad_t, pad_b = 44, 12, 18, 34
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    running = float(start)
    bars = []
    for name, delta in rows:
        bars.append((name, running, delta))
        running += delta
    lo = min([b[1] for b in bars] + [b[1] + b[2] for b in bars] + [0.0, running])
    hi = max([b[1] for b in bars] + [b[1] + b[2] for b in bars] + [0.0, running])
    span = (hi - lo) or 1.0
    body, step = [], plot_w / (len(bars) + 1)

    def Y(v: float) -> float:
        return pad_t + plot_h - (v - lo) / span * plot_h

    for tick in range(5):
        v = lo + span * tick / 4
        y = Y(v)
        body.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l + plot_w}" y2="{y:.1f}" '
                    f'stroke="{theme.BORDER}" opacity="0.45"/>')
        body.append(f'<text x="{pad_l - 6}" y="{y + 3:.1f}" text-anchor="end" '
                    f'style="font-size:9.5px;fill:{theme.FAINT}">{fmt_num(v)}</text>')
    for i, (name, base, delta) in enumerate(bars):
        cx = pad_l + step * (i + 0.5)
        y0, y1 = Y(base), Y(base + delta)
        top, h = min(y0, y1), max(1.0, abs(y1 - y0))
        color = theme.OK if delta >= 0 else theme.DANGER
        bw = min(52.0, step * 0.6)
        body.append(f'<rect x="{cx - bw / 2:.1f}" y="{top:.1f}" width="{bw:.1f}" '
                    f'height="{h:.1f}" rx="3" fill="{color}" opacity="0.85" '
                    f'data-tip="{esc(name)}：{delta:+,.4g}{esc(unit)}" '
                    f'class="wf" tabindex="0"/>')
        body.append(f'<text x="{cx:.1f}" y="{pad_t + plot_h + 14:.1f}" text-anchor="middle" '
                    f'style="font-size:10px;fill:{theme.MUTED}">{esc(name[:8])}</text>')
        if i < len(bars) - 1:
            body.append(f'<line x1="{cx + bw / 2:.1f}" y1="{Y(base + delta):.1f}" '
                        f'x2="{cx + step - bw / 2:.1f}" y2="{Y(base + delta):.1f}" '
                        f'stroke="{theme.FAINT}" stroke-dasharray="3 3"/>')
    # 合计柱
    cx = pad_l + step * (len(bars) + 0.5)
    bw = min(52.0, step * 0.6)
    y0, y1 = Y(0), Y(running)
    body.append(f'<rect x="{cx - bw / 2:.1f}" y="{min(y0, y1):.1f}" width="{bw:.1f}" '
                f'height="{max(1.0, abs(y1 - y0)):.1f}" rx="3" fill="{theme.ACCENT}" '
                f'data-tip="合计：{running:+,.4g}{esc(unit)}" class="wf" tabindex="0"/>')
    body.append(f'<text x="{cx:.1f}" y="{pad_t + plot_h + 14:.1f}" text-anchor="middle" '
                f'style="font-size:10px;fill:{theme.MUTED}">合计</text>')
    if label:
        body.append(f'<text x="{pad_l}" y="12" style="font-size:10.5px;'
                    f'fill:{theme.MUTED}">{esc(label)}</text>')
    aria = (f"瀑布图：起点 {fmt_num(start)}，"
            + "；".join(f"{k} {v:+,.4g}" for k, v in rows[:6])
            + f"，合计 {fmt_num(running)}{unit}")
    return svg(width, height, "".join(body), aria=aria)


# ========== 树图（构成占比，squarified）==========

def treemap(items: Sequence[tuple[str, float]], *, width: int = 720, height: int = 320,
            unit: str = "", filter_dim: str = "") -> str:
    """树图（squarified 简化版）：面积表示占比，适合渠道/来源构成"""
    rows = sorted(((str(k), float(v or 0)) for k, v in items if float(v or 0) > 0),
                  key=lambda t: -t[1])
    if not rows:
        return placeholder(width, height)
    total = sum(v for _, v in rows) or 1.0
    body: list[str] = []
    x = y = 0.0
    w, h = float(width), float(height)

    def place(idx: int, x0: float, y0: float, w0: float, h0: float) -> None:
        """递归二分：横向或纵向切一刀，保证长宽比不至于极端"""
        if idx >= len(rows) or w0 <= 0 or h0 <= 0:
            return
        if idx == len(rows) - 1:
            name, v = rows[idx]
            rect(x0, y0, w0, h0, name, v)
            return
        rest = sum(v for _, v in rows[idx:])
        take = rows[idx][1]
        frac = take / rest if rest else 1.0
        if w0 >= h0:                       # 竖切
            cut = w0 * frac
            rect(x0, y0, cut, h0, rows[idx][0], take)
            place(idx + 1, x0 + cut, y0, w0 - cut, h0)
        else:                              # 横切
            cut = h0 * frac
            rect(x0, y0, w0, cut, rows[idx][0], take)
            place(idx + 1, x0, y0 + cut, w0, h0 - cut)

    def rect(x0: float, y0: float, w0: float, h0: float, name: str, v: float) -> None:
        pct = v / total
        color = theme.series_color(len(body) % 8)
        cf = (f' data-cf="{esc(filter_dim)}:{esc(name)}" data-cf-label="{esc(name)}"'
              if filter_dim else "")
        body.append(f'<rect x="{x0 + 1:.1f}" y="{y0 + 1:.1f}" width="{max(0.0, w0 - 2):.1f}" '
                    f'height="{max(0.0, h0 - 2):.1f}" rx="4" fill="{color}" '
                    f'opacity="{0.35 + 0.55 * pct:.2f}"{cf} '
                    f'data-tip="{esc(name)}：{fmt_num(v)}{esc(unit)}（{pct:.1%}）" class="tm"/>')
        if w0 > 58 and h0 > 26:
            body.append(f'<text x="{x0 + 8:.1f}" y="{y0 + 18:.1f}" '
                        f'style="font-size:11px;fill:var(--on-accent);font-weight:600">'
                        f'{esc(name[:12])}</text>')
            body.append(f'<text x="{x0 + 8:.1f}" y="{y0 + 32:.1f}" '
                        f'style="font-size:10px;fill:var(--on-accent);opacity:.85">'
                        f'{fmt_num(v)} · {pct:.0%}</text>')

    place(0, x, y, w, h)
    aria = ("树图：" + "；".join(f"{k} {fmt_num(v)}（{v / total:.0%}）"
                                for k, v in rows[:6]))
    return svg(width, height, "".join(body), aria=aria)


# ========== 日历热力（按天，周为列）==========

def calendar_heatmap(values: Sequence[tuple[str, float]], *, width: int = 720,
                     cell: int = 14, label: str = "", unit: str = "") -> str:
    """日历热力（GitHub 风格）：values=[(YYYY-MM-DD, 值)]，周为列、周一为行首"""
    from datetime import datetime as _dt
    data: dict[str, float] = {}
    for k, v in values:
        key = str(k)[:10]
        data[key] = float(v or 0)
    if not data:
        return placeholder(width, 160)
    days = sorted(data)
    try:
        start = _dt.fromisoformat(days[0]).date()
        end = _dt.fromisoformat(days[-1]).date()
    except ValueError:
        return placeholder(width, 160)
    weeks = ((end - start).days // 7) + 2
    height = 7 * cell + 30
    vmax = max(data.values()) or 1
    body = []
    from datetime import timedelta as _td
    d = start - _td(days=start.weekday())
    col = 0
    while d <= end:
        for row in range(7):
            day = d + _td(days=row)
            if day < start or day > end:
                continue
            v = data.get(day.isoformat())
            x, y = 28 + col * (cell + 2), 18 + row * (cell + 2)
            if v is None:
                fill, op = theme.BORDER, 0.35
            else:
                fill, op = theme.ACCENT, 0.15 + 0.85 * min(1.0, v / vmax)
            body.append(f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" rx="3" '
                        f'fill="{fill}" opacity="{op:.2f}" class="cell" '
                        f'data-tip="{day.isoformat()}：'
                        f'{fmt_num(v) if v is not None else "无数据"}{esc(unit)}"/>')
        d += _td(days=7)
        col += 1
    for row, name in enumerate(("一", "三", "五", "日")):
        r = (0, 2, 4, 6)[row]
        body.append(f'<text x="4" y="{18 + r * (cell + 2) + cell - 3}" '
                    f'style="font-size:9px;fill:{theme.FAINT}">{name}</text>')
    if label:
        body.append(f'<text x="28" y="12" style="font-size:10.5px;'
                    f'fill:{theme.MUTED}">{esc(label)}</text>')
    aria = (f"日历热力（{len(data)} 天）：最高 "
            f"{fmt_num(max(data.values()))}{unit}")
    return svg(min(width, 28 + weeks * (cell + 2)), height, "".join(body), aria=aria)


# ========== K 线（开高低收）==========

def candlestick(bars: Sequence[tuple[str, float, float, float, float]], *,
                width: int = 720, height: int = 280, label: str = "") -> str:
    """K 线：bars=[(时间, 开, 高, 低, 收)]；用于价格/竞品定价区间监控"""
    rows = [(str(t), float(o), float(h), float(low), float(c))
            for t, o, h, low, c in bars]
    if not rows:
        return placeholder(width, height)
    pad_l, pad_r, pad_t, pad_b = 52, 12, 16, 28
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    lo = min(r[3] for r in rows)
    hi = max(r[2] for r in rows)
    span = (hi - lo) or 1.0
    step = plot_w / len(rows)
    bw = min(10.0, step * 0.6)
    body = []
    for tick in range(5):
        v = lo + span * tick / 4
        y = pad_t + plot_h - (v - lo) / span * plot_h
        body.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l + plot_w}" y2="{y:.1f}" '
                    f'stroke="{theme.BORDER}" opacity="0.45"/>')
        body.append(f'<text x="{pad_l - 6}" y="{y + 3:.1f}" text-anchor="end" '
                    f'style="font-size:9.5px;fill:{theme.FAINT}">{fmt_num(v)}</text>')
    for i, (t, o, h, low, c) in enumerate(rows):
        cx = pad_l + step * (i + 0.5)
        up = c >= o
        color = theme.OK if up else theme.DANGER

        def Y(v: float) -> float:
            return pad_t + plot_h - (v - lo) / span * plot_h

        body.append(f'<line x1="{cx:.1f}" y1="{Y(h):.1f}" x2="{cx:.1f}" '
                    f'y2="{Y(low):.1f}" stroke="{color}" stroke-width="1"/>')
        top, bottom = Y(max(o, c)), Y(min(o, c))
        body.append(f'<rect x="{cx - bw / 2:.1f}" y="{top:.1f}" width="{bw:.1f}" '
                    f'height="{max(1.0, bottom - top):.1f}" fill="{color}" class="k" '
                    f'data-tip="{esc(t)} 开{fmt_num(o)} 高{fmt_num(h)} '
                    f'低{fmt_num(low)} 收{fmt_num(c)}" tabindex="0"/>')
        if i % max(1, len(rows) // 8) == 0:
            body.append(f'<text x="{cx:.1f}" y="{pad_t + plot_h + 14:.1f}" '
                        f'text-anchor="middle" style="font-size:9.5px;fill:{theme.FAINT}">'
                        f'{esc(t[:8])}</text>')
    if label:
        body.append(f'<text x="{pad_l}" y="11" style="font-size:10.5px;'
                    f'fill:{theme.MUTED}">{esc(label)}</text>')
    aria = (f"K 线：{len(rows)} 根，区间 {fmt_num(lo)} ~ {fmt_num(hi)}，"
            f"最新收 {fmt_num(rows[-1][4])}")
    return svg(width, height, "".join(body), aria=aria)
