#!/usr/bin/env python3
"""抽取控制台页面里最大的内联 <script> 到文件（供 Node UI 行为回归使用）

用法：python scripts/extract_inline_js.py <html文件或URL> <输出js>
"""
import re
import sys
import urllib.request


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    src, out = sys.argv[1], sys.argv[2]
    if src.startswith("http"):
        with urllib.request.urlopen(src, timeout=20) as resp:   # noqa: S310
            html = resp.read().decode("utf-8", "ignore")
    else:
        with open(src, encoding="utf-8") as fh:
            html = fh.read()
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
    if not scripts:
        print("未找到内联脚本")
        return 1
    js = max(scripts, key=len)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(js)
    print(f"已抽取 {len(js)} 字符 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
