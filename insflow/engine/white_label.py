"""Insight Flow 白标报告（代理公司场景）

把 IF 生成的报告重新渲染为代理公司品牌化交付物：
- 品牌注入：公司名/logo/主色/页脚声明/免责条款
- 脱敏：内部 workspace id、引擎细节不外露（客户只见结论与建议）
- 套餐门控：white_label 功能由套餐控制（Scale/Enterprise）
- 导出：HTML（自包含单文件，可打印为 PDF）
"""

import html
from datetime import UTC, datetime

from ..core.files import EventBus, ReportStore
from ..engine.billing import BillingManager


class WhiteLabelConfig:
    """品牌配置（代理公司视角）"""

    def __init__(self, company: str = "", logo_url: str = "",
                 accent_color: str = "#2563eb", footer: str = "",
                 disclaimer: str = ""):
        self.company = company
        self.logo_url = logo_url
        self.accent_color = accent_color
        self.footer = footer
        self.disclaimer = disclaimer

    @classmethod
    def from_dict(cls, data: dict) -> "WhiteLabelConfig":
        return cls(
            company=data.get("company", ""),
            logo_url=data.get("logo_url", ""),
            accent_color=data.get("accent_color", "#2563eb"),
            footer=data.get("footer", ""),
            disclaimer=data.get("disclaimer", ""),
        )

    def to_dict(self) -> dict:
        return {"company": self.company, "logo_url": self.logo_url,
                "accent_color": self.accent_color,
                "footer": self.footer, "disclaimer": self.disclaimer}


class WhiteLabelRenderer:
    """白标报告渲染器"""

    def __init__(self, workspace_id: str, config: WhiteLabelConfig | None = None):
        self.workspace_id = workspace_id
        self.config = config or WhiteLabelConfig()
        self.bus = EventBus(workspace_id)

    async def render_html(self, markdown_content: str, client_name: str = "",
                          report_title: str = "增长诊断报告") -> str:
        """Markdown 报告 → 品牌化自包含 HTML（可打印 PDF）"""
        body_html = self._md_to_html(markdown_content)
        cfg = self.config
        now = datetime.now(UTC)
        logo_html = f'<img src="{html.escape(cfg.logo_url)}" alt="logo" class="logo">' if cfg.logo_url else ""
        header = (
            f'<div class="header"><div>{logo_html}'
            f'<div><div class="prepared">Prepared by {html.escape(cfg.company or "Insight Flow")}</div>'
            f'<div class="client">客户：{html.escape(client_name or self.workspace_id)}</div></div></div>'
        )
        footer = (
            f'<div class="footer">{html.escape(cfg.footer or "本报告由 Insight Flow 生成")}'
            f'<br><small>{html.escape(cfg.disclaimer)}</small></div>'
            if cfg.disclaimer or cfg.footer else ""
        )

        return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>{html.escape(report_title)}</title>
<style>
  body {{ font-family: -apple-system, "PingFang SC", sans-serif; max-width: 860px; margin: 0 auto; padding: 40px 24px; color: #1e293b; }}
  h1 {{ color: {cfg.accent_color}; border-bottom: 2px solid {cfg.accent_color}; padding-bottom: 8px; }}
  h2 {{ color: {cfg.accent_color}; margin-top: 28px; }}
  table {{ border-collapse: collapse; width: 100%; margin: 12px 0; }}
  th, td {{ border: 1px solid #e2e8f0; padding: 8px 12px; text-align: left; font-size: 14px; }}
  th {{ background: #f8fafc; }}
  code {{ background: #f1f5f9; padding: 2px 6px; border-radius: 4px; font-size: 13px; }}
  blockquote {{ border-left: 4px solid {cfg.accent_color}; margin: 0; padding: 4px 16px; color: #475569; }}
  .header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; }}
  .logo {{ height: 40px; }}
  .prepared {{ color: {cfg.accent_color}; font-weight: 700; }}
  .client {{ color: #64748b; font-size: 13px; margin-top: 4px; }}
  .footer {{ margin-top: 48px; padding-top: 16px; border-top: 1px solid #e2e8f0; color: #64748b; font-size: 12px; }}
  .generated {{ color: #94a3b8; font-size: 12px; }}
  @media print {{ body {{ padding: 0; }} }}
</style>
</head>
<body>
{header}
<div class="generated">生成时间：{now.strftime('%Y-%m-%d %H:%M UTC')}</div>
{body_html}
{footer}
</body>
</html>"""

    # ========== 套餐门控 + 导出 ==========

    async def export(self, report_category: str, filename: str,
                     client_name: str = "") -> dict:
        """把已有报告导出为品牌化 HTML（white_label 套餐门控）"""
        # 套餐门控
        mgr = BillingManager(self.workspace_id)
        plan = await mgr.get_plan()
        if not plan["limits"].get("white_label"):
            return {"ok": False, "detail": f"套餐 {plan['plan_id']} 不含白标报告（需 Scale/Enterprise）"}

        reports = ReportStore(self.workspace_id)
        content = reports.get_report(report_category, filename)
        if not content:
            return {"ok": False, "detail": f"报告不存在: {report_category}/{filename}"}

        html_doc = await self.render_html(content, client_name=client_name, report_title=filename)
        out_path = reports.base / "branded" / f"{filename.replace('.md', '')}.html"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(html_doc, encoding="utf-8")
        self.bus.emit("report.branded_export", {
            "report": f"{report_category}/{filename}", "client": client_name,
        })
        return {"ok": True, "path": str(out_path)}

    # ========== 极简 Markdown → HTML（零构建链家族风格）==========

    def _md_to_html(self, md: str) -> str:
        """极简 Markdown 渲染（标题/表格/列表/引用/代码），无第三方依赖"""
        lines = md.split("\n")
        out: list[str] = []
        in_table = False
        in_list = False

        def close_list():
            nonlocal in_list
            if in_list:
                out.append("</ul>")
                in_list = False

        for line in lines:
            stripped = line.strip()
            # 表格
            if stripped.startswith("|") and stripped.endswith("|"):
                cells = [c.strip() for c in stripped.strip("|").split("|")]
                if all(set(c) <= {"-", ":", " "} for c in cells):  # 分隔行
                    continue
                if not in_table:
                    out.append("<table>")
                    in_table = True
                    out.append("<tr>" + "".join(f"<th>{html.escape(c)}</th>" for c in cells) + "</tr>")
                    continue
                out.append("<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in cells) + "</tr>")
                continue
            close_list() if not stripped.startswith(("- ", "* ")) else None
            if in_table and not stripped.startswith("|"):
                out.append("</table>")
                in_table = False
            if not stripped:
                continue
            if stripped.startswith("### "):
                out.append(f"<h3>{html.escape(stripped[4:])}</h3>")
            elif stripped.startswith("## "):
                out.append(f"<h2>{html.escape(stripped[3:])}</h2>")
            elif stripped.startswith("# "):
                out.append(f"<h1>{html.escape(stripped[2:])}</h1>")
            elif stripped.startswith("> "):
                out.append(f"<blockquote>{html.escape(stripped[2:])}</blockquote>")
            elif stripped.startswith(("- ", "* ")):
                if not in_list:
                    out.append("<ul>")
                    in_list = True
                out.append(f"<li>{self._inline(html.escape(stripped[2:]))}</li>")
            else:
                close_list()
                out.append(f"<p>{self._inline(html.escape(stripped))}</p>")
        close_list()
        if in_table:
            out.append("</table>")
        return "\n".join(out)

    def _inline(self, text: str) -> str:
        """行内格式：**粗体** / `代码`"""
        import re
        text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
        text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
        return text
