"""看板快照：自包含 HTML（可打印 PDF）+ 可选服务端 PDF + 定时推送

设计取舍（零依赖约束下最诚实的做法）：
- **HTML 快照**：复用 `build_board_panels` 生成自包含页面（内联 SVG 图表 + 数据表），
  任何浏览器打开即可，`Ctrl/Cmd+P` 直接出 PDF —— 这条路径零依赖、永远可用。
- **服务端 PDF**：若环境有 headless Chrome/Chromium（`INSFLOW_CHROME_BIN` 或自动探测），
  调用 `--headless --print-to-pdf` 生成 PDF 一并留档；没有就明确返回 skipped，不假装能出 PDF。
- **推送**：按订阅（`mode=snapshot`）定期把快照链接发到飞书/邮件/Webhook（复用通知链路）。
- **留存**：快照默认保留 90 天，`cleanup()` 定期清理（可被隐私留存策略覆盖）。
"""

import os
import re
import shutil
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

DEFAULT_KEEP_DAYS = 90
CHROME_CANDIDATES = ("google-chrome", "google-chrome-stable", "chromium",
                     "chromium-browser", "chrome")


def snapshot_dir(workspace_id: str) -> Path:
    from ..core.files import DATA_DIR
    d = Path(DATA_DIR or ".") / "snapshots" / re.sub(r"[^\w.-]", "_", workspace_id)[:48]
    d.mkdir(parents=True, exist_ok=True)
    return d


def chrome_bin() -> str:
    """探测 headless Chrome（用于服务端出 PDF）；没有则返回空串"""
    explicit = os.environ.get("INSFLOW_CHROME_BIN", "").strip()
    if explicit:
        return explicit if Path(explicit).exists() else ""
    for name in CHROME_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    for mac in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Chromium.app/Contents/MacOS/Chromium"):
        if Path(mac).exists():
            return mac
    return ""


async def build_snapshot_html(workspace_id: str, panel: str = "board:traffic",
                              days: float = 30, title: str = "") -> str:
    """生成自包含快照 HTML（内联样式 + SVG 图表 + 数据表）"""
    from ..viz.base import TOKENS_CSS
    from ..viz.frame import datapanel
    from ..viz import charts as c  # noqa: F401  确保图表模块加载
    from ..web.routes import build_board_panels, _embed_normalize

    kind, _, name = panel.partition(":")
    panels_html: list[str] = []
    if kind == "metric":
        from ..core.store import get_store
        store = await get_store()
        series = await store.metric_series(workspace_id, name, days=days)
        rows = [[r["bucket"], r["value"]] for r in series]
        panels_html.append(datapanel(
            name, ["时间桶", "值"], rows,
            c.line_chart([{"name": name, "values": [r["value"] for r in series]}],
                         [r["bucket"] for r in series], as_area=True,
                         forecast_periods=7, anomaly=True),
            csv_name=f"snapshot-{name}", png=False))
        heading = name
    else:
        ws_dict = _embed_normalize(await _cockpit_data(name, workspace_id, days))
        for p in build_board_panels(name, ws_dict)[:6]:
            panels_html.append(datapanel(p["title"], p["columns"], p["rows"],
                                         p["chart"], csv_name=p["csv"], png=False))
        heading = name
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{(title or heading)} · Insight Flow 快照</title>
<style>
{TOKENS_CSS}
body{{padding:20px;max-width:1100px;margin:0 auto}}
.snap-head{{display:flex;justify-content:space-between;align-items:baseline;
  border-bottom:1px solid var(--border);padding-bottom:8px;margin-bottom:14px}}
.snap-title{{font-size:18px;font-weight:800}}
.snap-meta{{color:var(--faint);font-size:11.5px}}
@media print{{body{{padding:0}} .dp-tools{{display:none}}}}
</style></head><body>
<div class="snap-head">
  <div class="snap-title">{(title or heading)}</div>
  <div class="snap-meta">生成于 {generated} · 工作区 {workspace_id} · 自包含快照（可直接打印为 PDF）</div>
</div>
{''.join(panels_html)}
<div class="snap-meta" style="margin-top:16px">
  本文件为服务端生成的自包含快照，含内联样式与 SVG 图表，无外部依赖。
</div>
</body></html>"""


async def _cockpit_data(name: str, workspace_id: str, days: float) -> dict:
    from ..web.cockpit import COCKPITS
    fn = COCKPITS.get(name)
    if not fn:
        return {}
    try:
        return await fn(workspace_id, days)
    except TypeError:
        return await fn(workspace_id)


async def create_snapshot(workspace_id: str, panel: str = "board:traffic",
                          days: float = 30, title: str = "",
                          make_pdf: bool = True) -> dict:
    """生成快照并落盘；返回 {html_path, pdf_path?, size_kb, chrome}"""
    html = await build_snapshot_html(workspace_id, panel, days, title)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^\w.-]", "-", panel)[:40]
    out = snapshot_dir(workspace_id)
    html_path = out / f"{stamp}-{slug}.html"
    html_path.write_text(html, encoding="utf-8")
    result = {"panel": panel, "html_path": str(html_path),
              "html_rel": f"{workspace_id}/{html_path.name}",
              "size_kb": round(len(html) / 1024, 1),
              "created_at": datetime.now(UTC).isoformat()}
    if make_pdf:
        result.update(await _render_pdf(html_path))
    return result


async def _render_pdf(html_path: Path) -> dict:
    """有 Chrome 才出 PDF（否则明确 skipped，不伪造）"""
    chrome = chrome_bin()
    if not chrome:
        return {"pdf_skipped": "未检测到 headless Chrome（设 INSFLOW_CHROME_BIN 可启用；"
                              "临时可用浏览器打印为 PDF）"}
    pdf_path = html_path.with_suffix(".pdf")
    try:
        proc = subprocess.run(  # noqa: S603
            [chrome, "--headless", "--disable-gpu", "--no-sandbox",
             f"--print-to-pdf={pdf_path}", "--no-pdf-header-footer",
             html_path.as_uri()],
            capture_output=True, timeout=90)
        if pdf_path.exists() and pdf_path.stat().st_size > 0:
            return {"pdf_path": str(pdf_path),
                    "pdf_kb": round(pdf_path.stat().st_size / 1024, 1)}
        return {"pdf_skipped": f"Chrome 退出码 {proc.returncode}"}
    except Exception as e:                     # 绝不因为 PDF 失败而丢快照
        return {"pdf_skipped": f"{type(e).__name__}: {e}"}


def list_snapshots(workspace_id: str, limit: int = 50) -> list[dict]:
    out = []
    for f in sorted(snapshot_dir(workspace_id).glob("*.html"), reverse=True)[:limit]:
        pdf = f.with_suffix(".pdf")
        out.append({"name": f.name, "size_kb": round(f.stat().st_size / 1024, 1),
                    "created_at": datetime.fromtimestamp(f.stat().st_mtime, UTC).isoformat(),
                    "pdf": pdf.name if pdf.exists() else ""})
    return out


def resolve_snapshot(workspace_id: str, name: str) -> Path | None:
    """按名称解析快照文件（防目录穿越：只允许同目录文件名）"""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    path = snapshot_dir(workspace_id) / name
    return path if path.exists() else None


def cleanup(workspace_id: str, keep_days: int = DEFAULT_KEEP_DAYS) -> dict:
    """清理过期快照（HTML + PDF）"""
    cutoff = (datetime.now(UTC) - timedelta(days=keep_days)).timestamp()
    removed = 0
    freed = 0
    for f in snapshot_dir(workspace_id).iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            freed += f.stat().st_size
            f.unlink(missing_ok=True)
            removed += 1
    return {"removed": removed, "freed_mb": round(freed / 1024 / 1024, 2),
            "keep_days": keep_days}


async def push_snapshot(workspace_id: str, panel: str, days: float = 30,
                        title: str = "") -> dict:
    """生成快照并按工作区通知渠道推送链接"""
    snap = await create_snapshot(workspace_id, panel, days, title)
    base = os.environ.get("INSFLOW_BASE_PATH", "")
    link = f"{base}/console/snapshots/{snap['html_rel']}"
    from .alerts import _notify
    notified = await _notify(
        workspace_id, f"看板快照：{title or panel}",
        f"已生成快照（{snap['size_kb']}KB"
        + (f"，PDF {snap.get('pdf_kb', '')}KB" if snap.get("pdf_path") else "")
        + f"）：{link}", ["feishu", "webhook", "email"], {})
    return {**snap, "link": link, "notified": notified,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
