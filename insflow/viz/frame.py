"""Insight Flow 图表框架（P0：数据表切换 + 单图 CSV 导出）

零依赖：CSV 用 data-URI 内联（无需后端导出接口），表格服务端渲染隐藏，
前端只做显隐切换。所有图表外层可包 `datapanel()`，实现"每个图都能看数、能导出"。
"""

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


def _table_html(columns: list[str], rows: list[list]) -> str:
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in columns)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(v))}</td>" for v in row) + "</tr>"
        for row in rows
    )
    return f'<table class="dp-table"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def datapanel(title: str, columns: list[str], rows: list[list], chart_html: str, *,
              csv_name: str = "chart", subtitle: str = "", drill_metric: str = "",
              drill_entity: str = "") -> str:
    """图表面板：图/表切换 + CSV 导出（+ 可选下钻提示）

    columns/rows 同时用于表格渲染与 CSV 导出（单一数据源，不会图表与表格不一致）。
    """
    uid = f"dp{abs(hash(title + str(len(rows)))) % 100000:05d}"
    csv_uri = _csv_data_uri(columns, rows, csv_name)
    drill_attr = ""
    if drill_metric and drill_entity:
        drill_attr = (f' data-drill-metric="{html.escape(drill_metric)}"'
                      f' data-drill-entity="{html.escape(drill_entity)}"')
    return f'''<div class="dp" id="{uid}">
  <div class="dp-head">
    <div class="dp-title">{html.escape(title)}
      {f'<span class="dp-sub">{html.escape(subtitle)}</span>' if subtitle else ''}</div>
    <div class="dp-tools">
      <button type="button" class="dp-btn dp-on" onclick="dpView('{uid}','chart')" title="图表视图">图</button>
      <button type="button" class="dp-btn" onclick="dpView('{uid}','table')" title="数据视图">表</button>
      <a class="dp-btn" href="{csv_uri}" download="{html.escape(csv_name)}.csv" title="导出 CSV">CSV</a>
    </div>
  </div>
  <div class="dp-chart"{drill_attr}>{chart_html}</div>
  <div class="dp-tablewrap" hidden>{_table_html(columns, rows)}</div>
</div>'''
