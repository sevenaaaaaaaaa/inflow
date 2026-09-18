"""Insight Flow 图表框架（数据表切换 + CSV / XLSX / PNG 导出）

零依赖：CSV 与 XLSX 用 data-URI 内联（无需后端导出接口），PNG 由前端把
SVG/Canvas 光栅化后下载；表格服务端渲染隐藏，前端只做显隐切换。
所有图表外层可包 `datapanel()`，实现"每个图都能看数、能导出"。
"""

import base64
import csv
import html
import io
from urllib.parse import quote


def _csv_data_uri(columns: list[str], rows: list[list], stem: str) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    for row in rows:
        writer.writerow(row)
    payload = quote(buf.getvalue(), safe="")
    return f"data:text/csv;charset=utf-8,{payload}"


def _numeric_columns(columns: list[str], rows: list[list]) -> set[int]:
    """整列可数值化（≥80% 且非空）→ 该列右对齐并启用等宽数字"""
    numeric: set[int] = set()
    for idx in range(len(columns)):
        values = [row[idx] for row in rows[:200]
                  if idx < len(row) and row[idx] not in (None, "")]
        if not values:
            continue
        ok = 0
        for v in values:
            try:
                float(str(v).replace(",", "").replace("%", ""))
                ok += 1
            except (TypeError, ValueError):
                continue
        if ok / len(values) >= 0.8:
            numeric.add(idx)
    return numeric


def _table_html(columns: list[str], rows: list[list], caption: str = "") -> str:
    """无障碍数据表：scope=col + caption + 数字列右对齐（屏幕阅读器可读）"""
    numeric = _numeric_columns(columns, rows)

    def _cls(i: int) -> str:
        return ' class="num"' if i in numeric else ""

    head = "".join(f'<th scope="col"{_cls(i)}>{html.escape(str(c))}</th>'
                   for i, c in enumerate(columns))
    body = "".join(
        "<tr>" + "".join(f"<td{_cls(i)}>{html.escape(str(v))}</td>"
                         for i, v in enumerate(row)) + "</tr>"
        for row in rows
    )
    cap = f'<caption class="sr-only">{html.escape(caption)}</caption>' if caption else ""
    return f'<table class="dp-table">{cap}<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def _xlsx_data_uri(columns: list[str], rows: list[list], name: str,
                   limit: int = 5000) -> str:
    """XLSX 内联（stdlib zipfile；超限返回空串，避免 HTML 体积失控）"""
    if len(rows) > limit:
        return ""
    from ..core.xlsx import write_xlsx
    try:
        data = write_xlsx([(name or "data", columns, rows)])
    except Exception:
        return ""
    b64 = base64.b64encode(data).decode()
    return ("data:application/vnd.openxmlformats-officedocument."
            f"spreadsheetml.sheet;base64,{b64}")


def datapanel(title: str, columns: list[str], rows: list[list], chart_html: str, *,
              csv_name: str = "chart", subtitle: str = "", drill_metric: str = "",
              drill_entity: str = "", xlsx: bool = True, png: bool = True,
              panel_key: str = "") -> str:
    """图表面板：图/表切换 + CSV / XLSX / PNG 导出（+ 可选下钻提示）

    columns/rows 同时用于表格渲染与导出（单一数据源，不会图表与表格不一致）。
    """
    import re as _re
    # 面板稳定标识：优先显式 panel_key，否则由标题推导（同页同名面板需显式传 key）
    key = panel_key or (csv_name if csv_name and csv_name != "chart" else
                        _re.sub(r"[^\w\u4e00-\u9fff-]+", "-", title)[:48] or "panel")
    uid = f"dp{key}"
    csv_uri = _csv_data_uri(columns, rows, csv_name)
    xlsx_uri = _xlsx_data_uri(columns, rows, csv_name) if xlsx else ""
    xlsx_btn = (f'<a class="dp-btn" href="{xlsx_uri}" '
                f'download="{html.escape(csv_name)}.xlsx" aria-label="导出 Excel" '
                f'title="导出 Excel（可继续透视/建模）">XLSX</a>') if xlsx_uri else ""
    png_btn = (f'<button type="button" class="dp-btn" '
               f'onclick="ifExportPNG(\'{uid}\',\'{html.escape(csv_name)}\')" '
               f'aria-label="导出 PNG" title="导出 PNG（图表图片，可直接贴报告）">PNG</button>'
               ) if png else ""
    drill_attr = ""
    if drill_metric and drill_entity:
        drill_attr = (f' data-drill-metric="{html.escape(drill_metric)}"'
                      f' data-drill-entity="{html.escape(drill_entity)}"')
    return f'''<div class="dp" id="{uid}" data-panel="{html.escape(key)}">
  <div class="dp-head">
    <div class="dp-title">{html.escape(title)}
      {f'<span class="dp-sub">{html.escape(subtitle)}</span>' if subtitle else ''}
      <button type="button" class="dp-btn" data-annot="{html.escape(key)}"
        aria-label="图表批注" title="批注（协作）"
        onclick="ifPanelComments(\'{html.escape(key)}\',this)">💬 <span class="annot-n">0</span></button>
    </div>
    <div class="dp-tools">
      <button type="button" class="dp-btn dp-on" aria-pressed="true" aria-label="图表视图"
        onclick="dpView('{uid}','chart')" title="图表视图">图</button>
      <button type="button" class="dp-btn" aria-pressed="false" aria-label="数据表视图"
        onclick="dpView('{uid}','table')" title="数据表视图">表</button>
      <a class="dp-btn" href="{csv_uri}" download="{html.escape(csv_name)}.csv"
        aria-label="导出 CSV" title="导出 CSV（当前数据表）">CSV</a>
      {xlsx_btn}
      {png_btn}
    </div>
  </div>
  <div class="dp-chart"{drill_attr}>{chart_html}</div>
  <div class="dp-tablewrap" hidden>{_table_html(columns, rows, title)}</div>
</div>'''
