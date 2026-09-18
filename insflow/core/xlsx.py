"""零依赖 XLSX 写出（stdlib zipfile）——交付用 Excel 导出

只写最小可用子集：内联字符串 + 数字单元格 + 粗体表头 + 冻结首行 + 列宽。
不引入 openpyxl（家族约束：零第三方依赖、私有化可离线）。
"""

import re
import zipfile
from io import BytesIO
from xml.sax.saxutils import escape

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
{sheets}</Types>"""

_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>
<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="2"><fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill></fills>
<borders count="1"><border/></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>
</styleSheet>"""

_INVALID_SHEET = re.compile(r"[\\/*?:\[\]]")


def _sheet_name(name: str, used: set[str]) -> str:
    clean = _INVALID_SHEET.sub("-", str(name or "Sheet")).strip()[:31] or "Sheet"
    base, i = clean, 2
    while clean.lower() in used:
        clean = f"{base[:28]}-{i}"
        i += 1
    used.add(clean.lower())
    return clean


def _col_ref(idx: int) -> str:
    """0 → A, 25 → Z, 26 → AA"""
    ref = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        ref = chr(65 + rem) + ref
    return ref


def _cell(col: int, row: int, value, *, bold: bool = False) -> str:
    ref = f"{_col_ref(col)}{row}"
    style = ' s="1"' if bold else ""
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"{style}><v>{value}</v></c>'
    text = escape(str(value))
    return (f'<c r="{ref}"{style} t="inlineStr"><is><t xml:space="preserve">'
            f'{text}</t></is></c>')


def _sheet_xml(columns: list, rows: list[list]) -> str:
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">']
    if columns:
        out.append('<sheetViews><sheetView workbookViewId="0">'
                   '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" '
                   'state="frozen"/></sheetView></sheetViews>')
        widths = []
        for i, cname in enumerate(columns):
            width = max(10, min(42, max(len(str(cname)) + 4,
                                        *(len(str(r[i])) + 2 for r in rows[:200]
                                          if i < len(r))) if rows else len(str(cname)) + 4))
            widths.append(f'<col min="{i + 1}" max="{i + 1}" width="{width}" customWidth="1"/>')
        out.append("<cols>" + "".join(widths) + "</cols>")
    out.append("<sheetData>")
    if columns:
        out.append('<row r="1">' + "".join(
            _cell(i, 1, cname, bold=True) for i, cname in enumerate(columns)) + "</row>")
    start = 2 if columns else 1
    for ridx, row in enumerate(rows, start=start):
        out.append(f'<row r="{ridx}">' + "".join(
            _cell(i, ridx, v) for i, v in enumerate(row)) + "</row>")
    out.append("</sheetData></worksheet>")
    return "".join(out)


def write_xlsx(sheets: list[tuple[str, list, list[list]]]) -> bytes:
    """sheets: [(表名, 列名列表, 行列表)] → xlsx 字节流（可直接作 download 响应）"""
    used: set[str] = set()
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        overrides = "".join(
            f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
            f'ContentType="application/vnd.openxmlformats-officedocument.'
            f'spreadsheetml.worksheet+xml"/>'
            for i in range(1, len(sheets) + 1)) if sheets else ""
        z.writestr("[Content_Types].xml", _CONTENT_TYPES.format(sheets=overrides))
        z.writestr("_rels/.rels", _RELS)
        names = [_sheet_name(name, used) for name, _, _ in sheets]
        wb = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
              'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
              "<sheets>"]
        for i, name in enumerate(names, start=1):
            wb.append(f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>')
        wb.append("</sheets></workbook>")
        z.writestr("xl/workbook.xml", "".join(wb))
        rels = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                'relationships">']
        for i in range(1, len(sheets) + 1):
            rels.append(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.'
                        f'org/officeDocument/2006/relationships/worksheet" '
                        f'Target="worksheets/sheet{i}.xml"/>')
        rels.append(f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.'
                    f'openxmlformats.org/officeDocument/2006/relationships/styles" '
                    f'Target="styles.xml"/>')
        rels.append("</Relationships>")
        z.writestr("xl/_rels/workbook.xml.rels", "".join(rels))
        z.writestr("xl/styles.xml", _STYLES)
        for i, (_, cols, rows) in enumerate(sheets, start=1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", _sheet_xml(list(cols), list(rows)))
    return buf.getvalue()
