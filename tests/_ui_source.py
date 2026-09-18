"""测试辅助：控制台 UI 源码（base.html + 抽离出的静态 JS）拼接读取

工程维护把 45KB 内联 JS / 13KB CSS 抽到了 `insflow/web/static_app.{js,css}` 以启用缓存，
因此「JS 钩子是否存在」类断言必须同时看模板与静态文件，否则测试会假失败。
"""
from pathlib import Path

BASE_HTML = Path("insflow/web/templates/base.html")
STATIC_JS = Path("insflow/web/static_app.js")
STATIC_CSS = Path("insflow/web/static_app.css")


def ui_source() -> str:
    """模板 + 静态 JS + 静态 CSS（任一不存在则跳过该项）"""
    parts = [p.read_text(encoding="utf-8") for p in (BASE_HTML, STATIC_JS, STATIC_CSS)
             if p.exists()]
    return "\n".join(parts)


def base_with_static() -> str:
    """别名：语义上等价于"控制台壳"的全部前端源码"""
    return ui_source()
