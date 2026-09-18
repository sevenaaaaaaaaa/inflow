"""Insight Flow 可视化基础样式（独立 HTML 报告复用）

控制台用 base.html 的样式；报告需要**自包含**（脱离控制台也能正确渲染），
因此把设计令牌与基础组件样式抽到这里，供报告渲染器内联。
"""

TOKENS_CSS = """
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:oklch(96.5% .016 85); --surface:oklch(100% 0 0 / .72); --surface-strong:oklch(100% 0 0 / .9);
  --fg:oklch(22% .02 70); --muted:oklch(46% .016 70); --faint:oklch(51% .014 75);
  --border:oklch(86% .014 80); --border-strong:oklch(76% .02 80); --hover-strong:oklch(22% .02 70 / .11);
  --accent:oklch(52% .17 258); --accent-strong:oklch(46% .17 258);
  --ok:oklch(58% .17 152); --warn:oklch(66% .15 75); --danger:oklch(55% .2 25);
  --r-md:18px; --r-sm:12px; --shadow-sm:0 10px 28px -14px oklch(30% .04 80 / .24);
  --grad:linear-gradient(135deg,oklch(52% .17 258),oklch(58% .16 285));
  --font:-apple-system,BlinkMacSystemFont,"PingFang SC","HarmonyOS Sans SC","MiSans","Segoe UI",system-ui,sans-serif;
  --mono:ui-monospace,'SF Mono','JetBrains Mono',Menlo,monospace;
}
[data-theme="dark"]{
  --bg:oklch(19% .014 70); --surface:oklch(27% .016 75 / .6); --surface-strong:oklch(30% .016 75 / .85);
  --fg:oklch(93% .008 85); --muted:oklch(72% .014 80); --faint:oklch(62% .012 80);
  --border:oklch(100% 0 0 / .12); --hover-strong:oklch(93% .008 85 / .13);
  --accent:oklch(74% .13 258); --ok:oklch(74% .15 152); --warn:oklch(76% .13 75); --danger:oklch(72% .16 25);
}
body{font-family:var(--font); color:var(--fg); background:var(--bg); line-height:1.6;
  padding:34px 30px; max-width:960px; margin:0 auto}
h1{font-size:25px; font-weight:800; letter-spacing:-.02em; margin:0 0 6px; color:var(--accent)}
h2{font-size:16.5px; font-weight:700; margin:26px 0 12px}
h3{font-size:14px; font-weight:700; margin:18px 0 8px}
p{margin:8px 0}
a{color:var(--accent); text-decoration:none}
table{width:100%; border-collapse:collapse; background:var(--surface); border:1px solid var(--border);
  border-radius:var(--r-md); overflow:hidden; font-size:13px; margin:12px 0}
th,td{padding:9px 13px; border-bottom:1px solid var(--border); text-align:left; vertical-align:top}
th{background:var(--hover-strong); font-weight:700; font-size:12px; color:var(--muted)}
tr:last-child td{border-bottom:0}
.card{background:var(--surface); border:1px solid var(--border); border-radius:var(--r-md);
  padding:18px; box-shadow:var(--shadow-sm); margin:10px 0}
.card.kpi{display:inline-block; min-width:150px; margin-right:10px}
.card .num{font-size:27px; font-weight:800; line-height:1.1; color:var(--accent)}
.card .label{color:var(--muted); font-size:12px; margin-top:4px}
.cards{display:grid; grid-template-columns:repeat(auto-fill,minmax(160px,1fr)); gap:12px; margin:14px 0}
code{background:var(--hover-strong); padding:2px 6px; border-radius:6px; font-family:var(--mono); font-size:12px}
blockquote{border-left:4px solid var(--accent); margin:10px 0; padding:4px 16px; color:var(--muted)}
ul{padding-left:22px} li{margin:4px 0}
.sub{color:var(--muted); font-size:12.5px}
.empty{color:var(--faint); padding:26px; text-align:center; background:var(--surface);
  border:1px dashed var(--border-strong); border-radius:var(--r-md)}
.rpt-head{display:flex; justify-content:space-between; align-items:flex-start; gap:16px;
  padding-bottom:14px; border-bottom:2px solid var(--accent); margin-bottom:18px}
.rpt-foot{margin-top:40px; padding-top:14px; border-top:1px solid var(--border);
  color:var(--muted); font-size:12px}
.toolbar{display:flex; gap:10px; flex-wrap:wrap; margin:12px 0}
.btn{display:inline-block; padding:8px 15px; border-radius:var(--r-sm); background:var(--accent);
  color:oklch(100% 0 0); font-weight:600; font-size:13px}
@media print{ body{padding:0; max-width:none} .toolbar{display:none} }
"""


def wrap_html(title: str, body: str, *, branding: dict | None = None,
              allow_theme_toggle: bool = True) -> str:
    """自包含 HTML 外壳（内联令牌样式 + 明暗主题 + 可选品牌头）"""
    b = branding or {}
    company = b.get("company", "Insight Flow")
    client = b.get("client", "")
    accent = b.get("accent_color", "")
    logo = (f'<img src="{b["logo_url"]}" alt="logo" style="height:38px">'
            if b.get("logo_url") else "")
    head = (f'<div class="rpt-head"><div>{logo}'
            f'<div style="font-weight:800;font-size:14px;color:var(--accent)">'
            f'Prepared by {company}</div>'
            + (f'<div class="sub">客户：{client}</div>' if client else "")
            + '</div><div class="toolbar">'
            + ('<button class="btn" onclick="window.print()" '
               'style="border:0;cursor:pointer">打印 / 导出 PDF</button>' if allow_theme_toggle else "")
            + ('<button class="btn" onclick="toggle()" style="background:var(--surface-strong);'
               'color:var(--fg);border:1px solid var(--border);cursor:pointer">切换主题</button>'
               if allow_theme_toggle else "")
            + "</div></div>")
    foot = (f'<div class="rpt-foot">{b.get("footer", "由 Insight Flow 生成")}'
            + (f'<br><small>{b.get("disclaimer", "")}</small>' if b.get("disclaimer") else "")
            + "</div>")
    accent_override = f"<style>:root{{--accent:{accent}}}</style>" if accent else ""
    script = ("<script>function toggle(){var d=document.documentElement;"
              "d.getAttribute('data-theme')==='dark'?d.removeAttribute('data-theme'):"
              "d.setAttribute('data-theme','dark');}</script>") if allow_theme_toggle else ""
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>{TOKENS_CSS}</style>{accent_override}</head><body>
{head}{body}{foot}{script}</body></html>"""
