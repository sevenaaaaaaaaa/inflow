/**
 * 列出洞察。运行：
 *   IF_BASE=http://127.0.0.1:8400 npx --yes tsx examples/typescript/list-insights.ts
 */
const BASE = (process.env.IF_BASE ?? "http://127.0.0.1:8400").replace(/\/$/, "");
const WS = process.env.IF_WORKSPACE ?? "insflow-demo";
const KEY = process.env.IF_API_KEY ?? "";

async function main(): Promise<void> {
  const headers: Record<string, string> = {};
  if (KEY) headers["X-API-Key"] = KEY;
  const res = await fetch(`${BASE}/api/v1/insights?workspace_id=${WS}&limit=5`, {
    headers,
  });
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}: ${await res.text()}`);
  }
  console.log(JSON.stringify(await res.json(), null, 2));
}

main().catch((err: unknown) => {
  console.error(err);
  process.exit(1);
});
