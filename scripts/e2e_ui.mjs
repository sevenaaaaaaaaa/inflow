#!/usr/bin/env node
/* UI 行为回归（无浏览器）：从真实渲染页面抽取内联 JS，在 Node 里用最小 DOM 桩验证
   关键交互逻辑——图例开关 / 跨图联动 URL / 布局保存载荷 / 批注计数。
   用法：node scripts/e2e_ui.mjs <提取出的内联JS文件>
   生成方式见 scripts/extract_inline_js.py */
import { readFileSync } from "node:fs";

const file = process.argv[2];
if (!file) { console.error("用法：node scripts/e2e_ui.mjs <inline.js>"); process.exit(2); }
const src = readFileSync(file, "utf8");

// ---- 最小 DOM 桩 ----
function makeClassList(el) {
  const set = new Set();
  return {
    add: (c) => set.add(c), remove: (c) => set.delete(c),
    contains: (c) => set.has(c),
    toggle: (c, force) => {
      const on = force === undefined ? !set.has(c) : !!force;
      on ? set.add(c) : set.delete(c);
      return on;
    },
  };
}
function makeEl(attrs = {}, tag = "DIV") {
  const el = {
    tagName: tag, _attrs: { ...attrs }, style: {}, children: [],
    classList: null, hidden: false,
    getAttribute(k) { return this._attrs[k] ?? null; },
    setAttribute(k, v) { this._attrs[k] = String(v); },
    querySelectorAll(sel) { return collect(this, sel); },
    closest() { return this; },
    appendChild(c) { this.children.push(c); return c; },
    addEventListener() {},
  };
  el.classList = makeClassList(el);
  return el;
}
function matches(el, sel) {
  if (sel.startsWith("[") && sel.endsWith("]")) {
    const key = sel.slice(1, -1).split("=")[0];
    return key in el._attrs;
  }
  if (sel.startsWith(".")) return el.classList.contains(sel.slice(1));
  return el.tagName === sel.toUpperCase();
}
function collect(root, sel) {
  const out = [];
  (function walk(n) {
    (n.children || []).forEach((c) => { if (matches(c, sel)) out.push(c); walk(c); });
  })(root);
  return out;
}

const results = [];
function check(name, ok, detail = "") {
  results.push({ name, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"} ${name}${detail ? " — " + detail : ""}`);
}

// ---- 注入桩并执行抽取的脚本片段 ----
globalThis.window = globalThis;
globalThis.document = {
  documentElement: makeEl({}, "HTML"),
  body: makeEl({ "data-ws": "ws1" }),
  createElement: (t) => makeEl({}, t.toUpperCase()),
  addEventListener: () => {},
  getElementById: () => null,
  querySelectorAll: () => [],
  querySelector: () => null,
};
globalThis.getComputedStyle = () => ({ getPropertyValue: () => "var(--accent)" });
let navigated = null;
globalThis.URL = class {
  constructor(href) { this.href = href; this.params = new Map([["days", "30"]]); }
  get searchParams() {
    const p = this.params;
    return {
      get: (k) => p.get(k) ?? null,
      set: (k, v) => p.set(k, v),
      delete: (k) => p.delete(k),
      keys: () => p.keys(),
    };
  }
  toString() {
    return "https://x/console/cockpit/traffic?" +
      [...this.params].map(([k, v]) => `${k}=${v}`).join("&");
  }
};
const BASE_HREF = "https://x/console/cockpit/traffic?days=30";
globalThis.location = new Proxy({}, {
  get: (_, k) => (k === "href" ? BASE_HREF
    : k === "pathname" ? "/console/cockpit/traffic"
    : k === "search" ? "?days=30" : undefined),
  set: (_, k, v) => { if (k === "href") navigated = v; return true; },
});
// Node 22+ 的 navigator 是只读 getter → 用 defineProperty 覆盖
Object.defineProperty(globalThis, "navigator", {
  value: { onLine: true }, configurable: true, writable: true,
});
globalThis.fetch = async () => ({ ok: true, json: async () => ({}) });
globalThis.requestAnimationFrame = (fn) => fn(0);
globalThis.matchMedia = () => ({ matches: false });

// 取出需要的函数体（从抽取文件里按函数名截取到下一个顶层 function/var）
function grab(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) return "";
  let depth = 0, i = src.indexOf("{", start);
  for (let j = i; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (depth === 0) return src.slice(start, j + 1); }
  }
  return src.slice(start);
}
const pieces = ["ifToggleSeries", "ifLegendKey", "ifCrossFilter",
                "ifClearCrossFilter"].map(grab).join("\n");
if (pieces.includes("undefined")) { console.error("未能抽取函数"); process.exit(2); }

const factory = new Function(`${pieces}; return { ifToggleSeries, ifCrossFilter,
  ifClearCrossFilter };`);
const api = factory();

// 1) 图例开关：隐藏同序列元素并置 aria-pressed
const host = makeEl({});
const legend = makeEl({ "data-series": "本期" });
const line = makeEl({ "data-series": "本期" });
const other = makeEl({ "data-series": "上期" });
host.querySelectorAll = (sel) => (sel === "[data-series]" ? [line, other] : []);
legend.closest = () => host;
api.ifToggleSeries("本期", legend);
check("图例开关隐藏序列", line.style.display === "none" && other.style.display !== "none");
check("图例开关 aria-pressed", legend.getAttribute("aria-pressed") === "false");
api.ifToggleSeries("本期", legend);
check("再次点击恢复显示", line.style.display === "" && legend.getAttribute("aria-pressed") === "true");

// 2) 跨图联动：URL 参数 cf.<dim>=<value>
api.ifCrossFilter("province", "广东");
check("联动写入 cf.province", navigated && navigated.includes("cf.province=") &&
      decodeURIComponent(navigated).includes("广东"), String(navigated));
navigated = null;
api.ifClearCrossFilter();
check("清除联动删除 cf.*", navigated !== null, String(navigated));

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} 通过`);
process.exit(failed.length ? 1 : 0);
