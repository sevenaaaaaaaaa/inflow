"""导出水印与数据访问审计（企业合规）

- 水印：服务端导出（Excel/CSV）带上「公司 · 操作者 · 时间」——便于追责与防外流
- 访问审计：记录谁在何时导出了什么（进 admin_audit，action=access.export 等）
- 诚实边界：前端 PNG/SVG 在浏览器内生成，无法服务端加盖水印（页面已有只读/权限门控）
"""

from datetime import UTC, datetime

ACTIONS = {
    "xlsx": "access.export_xlsx",
    "csv": "access.export_csv",
    "embed": "access.embed_token",
    "report": "access.report_view",
}


def stamp(actor: str = "", company: str = "", at: str = "") -> str:
    """水印文本（统一格式，CSV/Excel/HTTP 头共用）"""
    when = at or datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    parts = [p for p in (company, actor, when) if p]
    return "导出水印：" + " · ".join(parts)


def csv_with_watermark(text: str, actor: str = "", company: str = "") -> str:
    """CSV 末尾追加水印行（以 # 开头，便于解析方忽略）"""
    mark = stamp(actor, company)
    if text and not text.endswith("\n"):
        text += "\n"
    # 首行也加头注释，抽样的 OCR/截图也能看到归属
    return f"# {mark}\n{text}# {mark}\n"


def xlsx_watermark_sheet(actor: str = "", company: str = "") -> tuple[str, list, list]:
    """Excel：追加「导出信息」工作表（返回 (表名, 列, 行)）"""
    return ("导出信息", ["项目", "值"], [
        ["公司", company or "—"],
        ["操作者", actor or "本地用户"],
        ["导出时间", datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")],
        ["说明", "本文件由 Insight Flow 导出，含操作者水印，请勿外传"],
    ])


def header(actor: str = "", company: str = "") -> dict:
    """HTTP 头水印（便于代理/网关侧记录）

    注意：HTTP 头必须是 latin-1 可编码，中文水印需百分号编码（RFC 3986），
    否则 starlette/h11 会直接抛 UnicodeEncodeError。
    """
    from urllib.parse import quote
    return {"X-Export-Watermark": quote(stamp(actor, company)[:180]),
            "X-Export-Actor": quote((actor or "local")[:120])}


def actor_of(request) -> str:
    user = getattr(getattr(request, "state", None), "user", None) or {}
    return str(user.get("email") or user.get("user_id") or "本地用户")


async def company_of(workspace_id: str) -> str:
    try:
        from ..core.store import get_store
        ws = await (await get_store()).get_workspace(workspace_id)
        if not ws:
            return ""
        branding = ((ws.settings_json or {}).get("branding") or {})
        return str(branding.get("company") or ws.name or "")
    except Exception:
        return ""
