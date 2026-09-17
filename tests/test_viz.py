"""测试 SVG 图表基座（P0-viz）"""

from insflow.viz import charts as c
from insflow.viz import primitives as p
from insflow.viz import theme


class TestPrimitives:
    def test_esc(self):
        assert p.esc('<a&b>') == "&lt;a&amp;b&gt;"

    def test_nice_max(self):
        assert p.nice_max(0) == 1.0
        assert p.nice_max(7) >= 7
        assert p.nice_max(1234) >= 1234

    def test_fmt_num(self):
        assert p.fmt_num(1234) == "1.2k"
        assert p.fmt_num(2_500_000) == "2.5M"
        assert p.fmt_num(42) == "42"
        assert p.fmt_num(None) == "-"

    def test_fmt_pct(self):
        assert p.fmt_pct(0.45) == "45%"
        assert p.fmt_pct(None) == "-"

    def test_line_points_single(self):
        pts, coords = p.line_points([5], 0, 0, 100, 50, 10)
        assert len(coords) == 1

    def test_placeholder(self):
        assert "暂无数据" in p.placeholder(100, 50)


class TestCharts:
    def test_kpi_card(self):
        html = c.kpi_card("新增洞察", 12, delta=0.2, spark=[1, 2, 3])
        assert "新增洞察" in html and "▲" in html and "svg" in html

    def test_kpi_negative_delta(self):
        html = c.kpi_card("流量", 90, delta=-0.15)
        assert "▼" in html

    def test_line_chart_multi(self):
        svg = c.line_chart(
            [{"name": "点击", "values": [1, 5, 3]}, {"name": "会话", "values": [2, 4, 6]}],
            ["周一", "周二", "周三"])
        assert svg.startswith("<svg") and "polyline" in svg
        assert "点击" in svg and "会话" in svg  # 图例

    def test_bar_chart_threshold(self):
        svg = c.bar_chart([("A", 0.5), ("B", 0.2)], threshold=0.35, threshold_label="阈值")
        assert "stroke-dasharray" in svg and "阈值" in svg

    def test_percent_bar(self):
        svg = c.percent_bar([("正", 3, theme.OK), ("负", 2, theme.DANGER)])
        assert svg.startswith("<svg") and "60%" in svg

    def test_stacked_bar(self):
        svg = c.stacked_bar([("w1", [("news", 5, theme.ACCENT), ("search", 3, theme.OK)])])
        assert "<rect" in svg

    def test_funnel_with_conversion(self):
        svg = c.funnel([("访问", 1000), ("加购", 200)])
        assert "20%" in svg  # 相邻步转化率

    def test_heatmap(self):
        svg = c.heatmap(["see", "think"], ["w1", "w2"], [[0.2, 0.9], [0.5, 0.1]])
        assert svg.count("<rect") >= 4

    def test_scatter_quadrant(self):
        svg = c.scatter([(100, 8, "kw1"), (900, 20, "kw2")], x_label="搜索量")
        assert "<circle" in svg and "搜索量" in svg

    def test_radar(self):
        svg = c.radar([("a", 0.5), ("b", 0.8), ("c", 0.3)])
        assert "polygon" in svg

    def test_gauge_color_by_value(self):
        assert theme.WARN in c.gauge(0.85)
        assert theme.DANGER in c.gauge(0.95)

    def test_timeline(self):
        html = c.timeline([{"ts": "2026-09-16T10:00", "title": "预警", "severity": "high"}])
        assert "预警" in html and "border-radius:50%" in html

    def test_tag_cloud_weight(self):
        html = c.tag_cloud([("退款", 9), ("崩溃", 3)])
        assert "退款" in html and "font-size" in html

    def test_all_empty_safe(self):
        """空数据不得抛异常（统一占位）"""
        assert c.line_chart([], []) is not None
        assert c.bar_chart([]) is not None
        assert c.percent_bar([]) is not None
        assert c.stacked_bar([]) is not None
        assert c.funnel([]) is not None
        assert c.heatmap([], [], []) is not None
        assert c.scatter([]) is not None
        assert c.radar([]) is not None
        assert c.timeline([]) is not None
        assert c.tag_cloud([]) is not None
        assert c.sparkline([]) is not None

    def test_html_escaped_in_labels(self):
        """标签转义（防 XSS）"""
        html = c.bar_chart([('<script>alert(1)</script>', 5)])
        assert "<script>" not in html
