"""Insight Flow CLI 入口"""

import asyncio
import contextlib
import json
import pathlib
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__

# 加载仓库 .env（CLI 也遵循同一份配置：驱动/MySQL/SMTP 等）
try:
    from pathlib import Path as _P

    from dotenv import load_dotenv as _ld
    _ld(_P(__file__).parent.parent / ".env")
except Exception:
    pass

console = Console()


def secrets_token() -> str:
    """生成主密钥（quickstart 首次运行用）"""
    import secrets
    return secrets.token_urlsafe(24)


def run_async(coro):
    """运行异步函数（结束后关闭全局 store，避免 aiosqlite 线程阻塞退出）"""
    from .core.store import close_store
    try:
        return asyncio.get_event_loop().run_until_complete(coro)
    finally:
        with contextlib.suppress(Exception):
            asyncio.get_event_loop().run_until_complete(close_store())


@click.group()
@click.version_option(version=__version__, prog_name="insflow")
def main():
    """Insight Flow - 增长情报与策略操作系统"""
    pass


@main.command("roi")
@click.option("--workspace", "-w", required=True)
def roi_cmd(workspace: str):
    """客户 ROI：有效动作 / 显著结论 / 成本与单位经济（OPC 视角）"""
    from .engine.roi import client_roi
    r = run_async(client_roi(workspace))
    o, c = r["output"], r["cost"]
    console.print(f"[bold]{r['workspace_id']}[/]  "
                  f"有效动作 {o['effective_actions']} · 显著 {o['significant_results']} · "
                  f"验证 {o['verified_total']}")
    console.print(f"  成本合计 ${c['total_cost_usd']}"
                  f"（数据源 ${c['source_cost_usd']} + LLM ${c['llm_cost_usd']}）"
                  f" · 接口 {c['api_calls']:.0f} 次 · Agent {c['agent_asks']:.0f} 次")
    per = r["unit_economics"]["cost_per_effective_action_usd"]
    console.print(f"  成本 / 有效动作：{'$' + str(per) if per is not None else '—（暂无有效动作）'}")


@main.command()
@click.option("--host", default="127.0.0.1", help="监听地址（对外暴露用 0.0.0.0）")
@click.option("--port", default=8400, type=int, help="服务端口")
@click.option("--workspace", "-w", default="insflow-demo", help="演示工作区 ID")
@click.option("--days", default=30, help="演示数据天数")
@click.option("--mock/--no-mock", "with_mock", default=True,
              help="灌入零依赖演示数据（无需任何 API Key）")
@click.option("--open/--no-open", "open_browser", default=True, help="启动后自动打开浏览器")
@click.option("--no-serve", is_flag=True, help="只准备数据不起服务（CI/脚本用）")
def quickstart(host: str, port: int, workspace: str, days: int, with_mock: bool,
               open_browser: bool, no_serve: bool):
    """一条命令跑起来：建配置 → 迁移 → （可选）灌演示数据 → 起服务

    面向首次使用/AI 爱好者：零 API Key 即可看到 9 个驾驶舱、即席探索、问数、
    行动验证与自进化的完整效果。
    """
    import pathlib

    root = pathlib.Path(__file__).parent.parent
    env_file = root / ".env"

    console.print("[bold]Insight Flow 快速开始[/]")
    # 1) 最小配置（首次运行自动生成；已有则不动）
    if not env_file.exists():
        env_file.write_text(
            "# Insight Flow 最小配置（quickstart 生成）\n"
            "INSFLOW_DB_DRIVER=sqlite\n"
            f"INSFLOW_MASTER_KEY={secrets_token()}\n",
            encoding="utf-8")
        console.print(f"  [green]✓[/] 已生成配置 {env_file}")
    else:
        console.print("  [dim]·[/] 复用已有 .env")

    # 2) 迁移
    from .core.store import reset_store

    async def _migrate():
        from .core.store import Store
        store = Store()
        await store.connect()
        await store.migrate()
        reset_store(store)
        from .core.entities import Workspace
        if not await store.get_workspace(workspace):
            await store.create_workspace(Workspace(id=workspace, name="演示工作区"))
        return store
    async def _prepare():
        store = await _migrate()
        count = 0
        if with_mock:
            from .engine.demo import DemoSeeder
            res = await DemoSeeder(workspace, days=days).seed()
            count = res.get("insights", 0)
        return store, count

    _store, insights = run_async(_prepare())
    console.print("  [green]✓[/] 数据库已就绪")

    if with_mock:
        console.print(f"  [green]✓[/] 演示数据：{insights} 条洞察"
                      f"（工作区 [bold]{workspace}[/]）")
        console.print("     零 API Key 即可体验：9 个驾驶舱 / 即席探索 / ⌘K 问数 / 行动验证 / 自进化")

    if no_serve:
        console.print("\n[green]准备完成[/]（--no-serve，未启动服务）")
        return

    # 3) 起服务（前台阻塞）
    url = f"http://{host if host != '0.0.0.0' else '127.0.0.1'}:{port}/console"
    console.print(f"\n[bold green]服务地址[/] {url}")
    console.print("[dim]按 Ctrl+C 停止[/]\n")
    if open_browser:
        import threading
        import webbrowser

        def _open():
            import time as _t
            _t.sleep(1.5)
            with contextlib.suppress(Exception):
                webbrowser.open(url)
        threading.Thread(target=_open, daemon=True).start()

    import uvicorn
    uvicorn.run("insflow.server.app:app", host=host, port=port, log_level="info")


@main.command()
@click.option("--port", default=8400, help="服务端口")
@click.option("--host", default="0.0.0.0", help="监听地址")
@click.option("--reload", is_flag=True, help="开发模式自动重载")
def serve(port: int, host: str, reload: bool):
    """启动 Insight Flow 服务"""
    import uvicorn
    console.print(f"[bold green]Starting Insight Flow on {host}:{port}[/]")
    uvicorn.run(
        "insflow.server.app:app",
        host=host,
        port=port,
        reload=reload,
    )


@main.command("mcp")
def mcp_stdio():
    """启动 MCP Server（stdio，给 Claude Desktop / Cursor 用）"""
    from .mcp_server.server import main as mcp_main
    mcp_main()


@main.group()
def workspace():
    """工作区管理"""
    pass


@workspace.command("list")
def workspace_list():
    """列出所有工作区"""
    from .core.store import get_store

    async def _list():
        store = await get_store()
        workspaces = await store.list_workspaces()
        if not workspaces:
            console.print("[yellow]No workspaces found[/]")
            return

        table = Table(title="Workspaces")
        table.add_column("ID", style="cyan")
        table.add_column("Name", style="green")
        table.add_column("Stage", style="magenta")
        table.add_column("Maturity", style="yellow")
        table.add_column("Created", style="dim")

        for ws in workspaces:
            table.add_row(
                ws.id, ws.name, ws.stage.value, ws.maturity_level.value,
                ws.created_at.strftime("%Y-%m-%d %H:%M")
            )
        console.print(table)

    run_async(_list())


@workspace.command("create")
@click.argument("name")
def workspace_create(name: str):
    """创建新工作区"""
    from .core.entities import Workspace
    from .core.store import get_store

    async def _create():
        store = await get_store()
        ws = Workspace(name=name)
        ws = await store.create_workspace(ws)
        console.print(f"[green]Created workspace: {ws.id} ({ws.name})[/]")

    run_async(_create())


@main.group()
def insight():
    """洞察管理"""
    pass


@insight.command("list")
@click.option("--workspace", "-w", required=True, help="工作区ID")
@click.option("--status", "-s", help="状态过滤")
@click.option("--severity", help="严重程度过滤")
@click.option("--limit", "-l", default=20, help="数量限制")
def insight_list(workspace: str, status: str, severity: str, limit: int):
    """列出洞察"""
    from .core.store import get_store

    async def _list():
        store = await get_store()
        insights = await store.list_insights(workspace, status=status, severity=severity, limit=limit)
        if not insights:
            console.print("[yellow]No insights found[/]")
            return

        table = Table(title=f"Insights (workspace: {workspace})")
        table.add_column("ID", style="cyan")
        table.add_column("Type", style="magenta")
        table.add_column("Title", style="green")
        table.add_column("Severity", style="red")
        table.add_column("Confidence", style="yellow")
        table.add_column("Status", style="blue")
        table.add_column("Created", style="dim")

        for ins in insights:
            table.add_row(
                ins.id, ins.type, ins.title[:40], ins.severity.value,
                f"{ins.confidence:.0%}", ins.status.value,
                ins.created_at.strftime("%Y-%m-%d %H:%M")
            )
        console.print(table)

    run_async(_list())


@main.group()
def plugin():
    """插件管理"""
    pass


@main.group()
def events():
    """事件目录（伙伴契约）"""
    pass


@events.command("catalog")
@click.option("--write", "do_write", is_flag=True, help="回写 docs/14-事件目录.md")
@click.option("--direction", default="", help="inbound|outbound|internal")
@click.option("--json", "as_json", is_flag=True, help="输出 JSON")
def events_catalog(do_write: bool, direction: str, as_json: bool):
    """打印或回写事件目录（与 GET /api/v1/events/catalog 同源）"""
    from .core.event_catalog import DIRECTIONS, catalog, render_markdown, write_docs
    if direction and direction not in DIRECTIONS:
        console.print(f"[red]direction 必须是 {list(DIRECTIONS)}[/]")
        sys.exit(1)
    if do_write:
        path = write_docs()
        console.print(f"[green]已写入[/] {path}")
        return
    if as_json or direction:
        console.print_json(data=catalog(direction or None))
        return
    console.print(render_markdown())


@main.group("prompt")
def prompt_group():
    """Prompt 版本与回归评测"""
    pass


@prompt_group.command("list")
def prompt_list():
    """列出 prompts/ 下的 prompt 与版本"""
    from .agent.prompts import list_prompts, prompts_dir
    rows = list_prompts()
    if not rows:
        console.print(f"[yellow]{prompts_dir()} 下没有 prompt[/]（代码内置兜底仍可用）")
        return
    for r in rows:
        console.print(f"[bold]{r['name']}[/]  版本 {r['versions']} · 生效 v{r['latest']}")


@prompt_group.command("show")
@click.argument("name", default="agent_system")
@click.option("--version", "-v", type=int, default=None)
def prompt_show(name: str, version: int | None):
    """打印某一版 prompt 正文（含哈希，便于和答案里的 ref 对账）"""
    from .agent.agent import SYSTEM_PROMPT
    from .agent.prompts import get_prompt
    p = get_prompt(name, version, fallback=SYSTEM_PROMPT if name == "agent_system" else "")
    console.print(f"[bold]{p.ref}[/]  {p.path or '（代码内置兜底）'}")
    console.print(p.text)


@prompt_group.command("eval")
@click.option("--workspace", "-w", required=True)
@click.option("--version", "-v", "versions", multiple=True, type=int,
              help="要评测的版本（可多次；不填用生效版本）")
@click.option("--set", "set_name", default="agent_qa", help="golden set 名")
@click.option("--min-score", default=0.0, type=float, help="低于该分数退出码为 1（CI 门禁）")
@click.option("--write", "do_write", is_flag=True, help="报告写入 data/prompt_evals/")
@click.option("--json", "as_json", is_flag=True)
def prompt_eval(workspace: str, versions: tuple, set_name: str,
                min_score: float, do_write: bool, as_json: bool):
    """跑 golden set 回归（LLM 未配置时只验证答案管线，报告会标注）"""
    from .agent.prompt_eval import compare, run_eval
    if len(versions) > 1:
        result = run_async(compare(workspace, list(versions), set_name=set_name))
        if as_json:
            console.print_json(data=result)
            return
        for row in result["versions"]:
            console.print(f"v{row['version']}: {row['score']:.0%} "
                          f"（{row['passed']}/{row['total']}）  {row['prompt']}")
        if not result["prompt_sensitive"]:
            console.print("[yellow]提示[/]：未配置 LLM，各版本走同一条确定性管线，"
                          "分数相同属正常——配 OPENAI_API_KEY 才能真正对比 prompt")
        score = min((r["score"] for r in result["versions"]), default=0.0)
    else:
        report = run_async(run_eval(workspace, version=(versions[0] if versions else None),
                                    set_name=set_name, write=do_write))
        if as_json:
            console.print_json(data=report)
            return
        console.print(f"[bold]{report['prompt']}[/] · 模式 {report['mode']} · "
                      f"得分 {report['score']:.0%}（{report['passed']}/{report['total']}"
                      f"，跳过 {report['skipped']}）")
        for case in report["cases"]:
            if case.get("skipped"):
                continue
            mark = "[green]✓[/]" if case.get("passed") else "[red]✗[/]"
            console.print(f"  {mark} {case['id']} {'; '.join(case.get('reasons') or [])}")
        if not report["prompt_sensitive"]:
            console.print("[yellow]提示[/]：未配置 LLM → 本轮测的是答案管线不退化，"
                          "prompt 文案差异测不出来")
        if report.get("path"):
            console.print(f"报告：{report['path']}")
        score = report["score"]
    if min_score and score < min_score:
        console.print(f"[red]低于门槛 {min_score:.0%}[/]")
        sys.exit(1)


@main.command("search")
@click.argument("query")
@click.option("--workspace", "-w", required=True)
@click.option("--kind", "kinds", multiple=True,
              type=click.Choice(["insight", "note"]), help="检索范围（可多次）")
@click.option("--limit", "-l", default=10, help="返回条数")
def search_cmd(query: str, workspace: str, kinds: tuple, limit: int):
    """语义检索洞察/工作区记忆（本地向量，词面不命中也能召回）"""
    from .engine.semantic_index import search
    hits = run_async(search(workspace, query, kinds=list(kinds) or None, limit=limit))
    if not hits:
        console.print("[yellow]没有命中[/]（索引为空时先跑一次诊断或 `insflow index rebuild`）")
        return
    for h in hits:
        console.print(f"[bold]{h['score']:.3f}[/] [{h['kind']}] {h['title']} "
                      f"[dim]{h['ref_id']}[/]")
        console.print(f"  {h['snippet'][:120]}")


@main.group("index")
def index_group():
    """语义检索索引"""
    pass


@index_group.command("rebuild")
@click.option("--workspace", "-w", required=True)
def index_rebuild(workspace: str):
    """全量重建向量索引（换 embedding 模型后必须跑）"""
    from .engine.semantic_index import reindex
    r = run_async(reindex(workspace))
    console.print(f"[green]已重建[/] 索引 {r['indexed']} 条（清理孤儿 {r['removed']}）")


@index_group.command("stats")
@click.option("--workspace", "-w", required=True)
def index_stats(workspace: str):
    """索引统计（条数/模型/维度）"""
    from .engine.semantic_index import stats
    st = run_async(stats(workspace))
    console.print(f"总计 {st['total']} 条 · 维度 {st['dim']} · "
                  f"模型 {', '.join(st['models']) or '—'}")
    for kind, n in st["by_kind"].items():
        console.print(f"  {kind}: {n}")


@plugin.command("check")
@click.argument("plugin_path")
def plugin_check(plugin_path: str):
    """检查插件是否合规（与市场安装同源校验器）"""
    from .engine.marketplace import check_plugin
    report = check_plugin(Path(plugin_path))
    if report["passed"]:
        console.print("[bold green]Plugin check PASSED[/]")
        for warn in report.get("warnings") or []:
            console.print(f"  [yellow]⚠[/] {warn}")
    else:
        console.print("[bold red]Plugin check FAILED[/]")
        for err in report.get("errors") or []:
            console.print(f"  [red]✗[/] {err}")
        sys.exit(1)


@plugin.command("new")
@click.argument("ptype", type=click.Choice(["source", "model", "action", "template"]))
@click.argument("plugin_id")
@click.option("--out", "out_dir", default="", help="父目录（默认 plugins/<type>s）")
@click.option("--name", default="", help="显示名")
@click.option("--force", is_flag=True, help="覆盖已存在目录")
def plugin_new(ptype: str, plugin_id: str, out_dir: str, name: str, force: bool):
    """生成插件脚手架（manifest + entry，可通过 plugin check）"""
    from .engine.plugin_scaffold import create_plugin, default_dest
    dest = default_dest(ptype, plugin_id, Path(out_dir) if out_dir else None)
    result = create_plugin(ptype, plugin_id, dest=dest, name=name, force=force)
    if not result["ok"]:
        console.print(f"[red]{result.get('error')}[/]")
        sys.exit(1)
    check = result["check"]
    console.print(f"[green]已生成[/] {result['path']}")
    if check.get("passed"):
        console.print("[green]plugin check PASSED[/]")
    else:
        console.print("[yellow]plugin check 未通过（目录已写出，请按错误改）[/]")
        for err in check.get("errors") or []:
            console.print(f"  [red]✗[/] {err}")
        sys.exit(1)


@plugin.command("install")
@click.argument("plugin_path")
def plugin_install(plugin_path: str):
    """从本地市场安装插件（自动 plugin check）"""
    from .engine.marketplace import Marketplace
    market = Marketplace()
    result = market.install(plugin_path)
    if result["ok"]:
        console.print(f"[green]已安装: {result['plugin_id']} ({result['type']})[/]")
        if not result.get("auto_scheduled"):
            console.print("[yellow]⚠ 未通过 check，不会被调度器自动执行[/]")
        for w in result.get("warnings", []):
            console.print(f"  [yellow]⚠[/] {w}")
    else:
        console.print(f"[red]安装失败（{result.get('stage')}）[/]")
        for err in result.get("errors", []):
            console.print(f"  [red]✗[/] {err}")
        sys.exit(1)


@plugin.command("uninstall")
@click.argument("plugin_id")
def plugin_uninstall(plugin_id: str):
    """卸载插件"""
    from .engine.marketplace import Marketplace
    result = Marketplace().uninstall(plugin_id)
    if result["ok"]:
        console.print(f"[green]已卸载: {plugin_id}[/]")
    else:
        console.print(f"[red]{result.get('detail')}[/]")


@plugin.command("market")
def plugin_market():
    """列出本地市场中可安装的插件"""
    from .engine.marketplace import Marketplace
    items = Marketplace().scan()
    if not items:
        console.print("[yellow]marketplace/ 目录暂无可安装插件[/]")
        return
    table = Table(title="Plugin Marketplace")
    table.add_column("ID", style="cyan")
    table.add_column("Type", style="magenta")
    table.add_column("Name", style="green")
    table.add_column("Version", style="dim")
    table.add_column("Check", style="bold")
    for it in items:
        table.add_row(it["id"], it["type"], it["name"], it["version"],
                      "[green]✓[/]" if it["check_passed"] else "[red]✗[/]")
    console.print(table)


@main.group()
def run():
    """运行管线（诊断/验证评估）"""
    pass


@run.command("diagnosis")
@click.option("--workspace", "-w", required=True, help="工作区ID")
def run_diagnosis(workspace: str):
    """跑一次全量诊断管线（AARRR + 异常检测 + 洞察 + 报告）"""
    from .core.store import get_store
    from .engine.diagnosis import DiagnosisEngine

    async def _run():
        store = await get_store()
        if not await store.get_workspace(workspace):
            console.print(f"[red]Workspace 不存在: {workspace}[/]")
            sys.exit(1)
        engine = DiagnosisEngine(workspace)
        result = await engine.run()
        console.print(f"[green]诊断完成[/] 新增洞察 {result['insights_created']} 条")
        if result["quality_gate_failed"]:
            console.print(f"[yellow]质量门拦截 {len(result['quality_gate_failed'])} 条草稿[/]")
        console.print(f"报告: {result['report_path']}")
        return result

    run_async(_run())


@main.group()
def agent():
    """Agent 问答"""
    pass


@agent.command("ask")
@click.argument("question")
@click.option("--workspace", "-w", default="default", help="工作区ID")
def agent_ask(question: str, workspace: str):
    """数据洞察问答（无 OPENAI_API_KEY 时 retrieval 模式）"""
    from .agent import InsightAgent

    async def _ask():
        agent = InsightAgent(workspace)
        result = await agent.ask(question)
        console.print(Panel(result["answer"], title=f"mode={result['mode']}"))
        if result["citations"]:
            console.print("[dim]引用溯源：[/]")
            for c in result["citations"][:10]:
                console.print(f"  [dim]ins:{c['insight_id']} {c['title']}[/]")
        for p in result.get("proposed_actions") or []:
            console.print(f"  [yellow]待审批动作[/] {p['action_id']} "
                          f"{p['action_type']}（{p['state']}）—— "
                          f"批准：insflow action approve {p['action_id']} -w {workspace}")
        return result

    run_async(_ask())


@agent.command("notes")
@click.option("--workspace", "-w", default="default", help="工作区ID")
@click.option("--query", "-q", default="", help="关键词检索（空=最近）")
def agent_notes(workspace: str, query: str):
    """列出 / 检索工作区记忆（agent_notes）"""
    from .engine.agent_memory import list_notes, recall_notes

    async def _run():
        return await (recall_notes(workspace, query) if query.strip()
                      else list_notes(workspace))

    notes = run_async(_run())
    console.print(f"[green]{len(notes)} 条记忆[/]")
    for n in notes:
        cites = "、".join(str(c.get("insight_id") or c)
                           for c in (n.get("citations_json") or [])[:3])
        console.print(f"  - [bold]{n['title']}[/] [dim]v{n.get('version', 1)}[/]"
                      + (f"（引用 {cites}）" if cites else ""))
        console.print(f"    [dim]{(n.get('body') or '')[:100]}[/]")


@agent.command("remember")
@click.argument("text")
@click.option("--title", "-t", required=True, help="记忆标题（同名更新，版本+1）")
@click.option("--workspace", "-w", default="default", help="工作区ID")
def agent_remember(text: str, title: str, workspace: str):
    """写入工作区记忆（问答与定时任务都会带上）"""
    from .engine.agent_memory import save_note

    note = run_async(save_note(workspace, title=title, body=text, author="cli"))
    console.print(f"[green]已保存[/] {note.get('note_key')} v{note.get('version')} "
                  f"({note.get('id')})")


@agent.command("tasks")
@click.option("--workspace", "-w", default="default", help="工作区ID")
def agent_tasks(workspace: str):
    """列出 Agent 定时任务"""
    from .core.store import get_store

    async def _run():
        return await (await get_store()).list_agent_tasks(workspace)

    tasks = run_async(_run())
    console.print(f"[green]{len(tasks)} 个定时任务[/]")
    for t in tasks:
        state = "启用" if t["enabled"] else "暂停"
        console.print(f"  - [{state}] {t['id']} [bold]{t['name']}[/] cron={t['cron']} "
                      f"last={t.get('last_status') or '—'}"
                      + (f" runs={t.get('run_count', 0)}" if t.get("run_count") else ""))
        console.print(f"    [dim]{t['question'][:100]}[/]")


@agent.command("task-add")
@click.argument("question")
@click.option("--cron", default="daily", help="hourly/daily/weekly 或 5 段式（UTC）")
@click.option("--name", default="", help="任务名（默认取问题前 40 字）")
@click.option("--workspace", "-w", default="default", help="工作区ID")
def agent_task_add(question: str, cron: str, name: str, workspace: str):
    """把一个问题固化为定时任务（跑完推送飞书/Webhook）"""
    from .engine.agent_tasks import create_task

    task = run_async(create_task(workspace, name=name, question=question,
                                 cron=cron, created_by="cli"))
    console.print(f"[green]已创建[/] {task['id']} cron={task['cron']} "
                  + ("（已挂到调度器）" if task.get("scheduled") else "（调度器未运行，启动后生效）"))


@agent.command("task-run")
@click.argument("task_id")
def agent_task_run(task_id: str):
    """立即执行一次 Agent 定时任务"""
    from .engine.agent_tasks import run_task

    result = run_async(run_task(task_id))
    if result.get("ok"):
        console.print(Panel(result.get("answer") or "", title=f"mode={result.get('mode')}"))
        console.print(f"[green]已执行[/] 推送：{result.get('pushed') or '（未配置通道）'}")
    else:
        console.print(f"[red]执行失败：{result.get('error')}[/]")
        sys.exit(1)


@agent.command("task-rm")
@click.argument("task_id")
@click.option("--workspace", "-w", required=True, help="工作区ID")
def agent_task_rm(task_id: str, workspace: str):
    """删除 Agent 定时任务（同时摘除调度 job）"""
    from .engine.agent_tasks import delete_task

    ok = run_async(delete_task(workspace, task_id))
    console.print("[green]已删除[/]" if ok else "[red]任务不存在[/]")
    if not ok:
        sys.exit(1)


@main.group()
def report():
    """报告生成"""
    pass


@report.command("weekly")
@click.option("--workspace", "-w", required=True, help="工作区ID")
def report_weekly(workspace: str):
    """生成增长周报（+ 自动同步 MFlow 报告目录）"""
    from .engine.weekly_report import WeeklyReportBuilder

    async def _run():
        result = await WeeklyReportBuilder(workspace).build()
        console.print("[green]周报已生成[/]")
        console.print(f"本地: {result['report_path']}")
        if result.get("mflow_path"):
            console.print(f"MFlow: {result['mflow_path']}")

    run_async(_run())


@main.command()
@click.option("--workspace", "-w", required=True, help="工作区ID")
@click.option("--out", "-o", default="backup", help="导出目录")
@click.option("--format", "-f", "fmt", default="json", type=click.Choice(["json", "csv", "md"]))
def export(workspace: str, out: str, fmt: str):
    """全量导出（防锁定承诺：数据随时可带走）"""
    import json as jsonlib
    from datetime import datetime
    from pathlib import Path

    async def _run():
        from .core.store import get_store
        store = await get_store()
        ws = await store.get_workspace(workspace)
        if not ws:
            console.print("[red]Workspace 不存在[/]")
            sys.exit(1)

        out_dir = Path(out) / workspace
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d")

        data = {
            "exported_at": datetime.now().isoformat(),
            "workspace": ws.model_dump(mode="json"),
            "insights": [i.model_dump(mode="json")
                         for i in await store.list_insights(workspace, limit=10000)],
            "actions": [a.model_dump(mode="json")
                        for a in await store.list_actions(workspace)],
            "feedback_stats": await store.get_feedback_stats(workspace),
            "model_effectiveness": await store.get_model_effectiveness(workspace),
            "competitors": await store.list_competitors(workspace),
            "journey_events": await store.list_journey_events(workspace, limit=10000),
        }

        if fmt == "json":
            path = out_dir / f"insflow-export-{stamp}.json"
            path.write_text(jsonlib.dumps(data, ensure_ascii=False, indent=2, default=str),
                            encoding="utf-8")
        elif fmt == "md":
            lines = [f"# Insight Flow 导出 · {workspace}", ""]
            lines.append(f"导出时间：{data['exported_at']}")
            lines += ["", "## 洞察", ""]
            for ins in data["insights"]:
                lines.append(f"- [{ins['severity']}] {ins['title']} (ins:{ins['id']})")
            path = out_dir / f"insflow-export-{stamp}.md"
            path.write_text("\n".join(lines), encoding="utf-8")
        else:
            import csv
            path = out_dir / f"insflow-export-{stamp}.csv"
            # CLI 单次导出：同步写文件可接受（不与请求并发；见 docs/11 工程规范）
            with open(path, "w", newline="", encoding="utf-8") as fh:  # noqa: ASYNC230
                writer = csv.writer(fh)
                writer.writerow(["id", "type", "title", "severity", "confidence", "status", "created_at"])
                for ins in data["insights"]:
                    writer.writerow([ins["id"], ins["type"], ins["title"],
                                     ins["severity"], ins["confidence"], ins["status"], ins["created_at"]])

        # 导出审计（R3-5：全量导出写审计事件，防数据外带无痕）
        from .core.files import EventBus
        EventBus(workspace).emit("data.exported", {
            "workspace_id": workspace, "format": fmt,
            "path": str(path), "insights": len(data["insights"]),
        })
        console.print(f"[green]已导出 {len(data['insights'])} 条洞察 → {path}[/]")
        console.print("[dim]已记录导出审计事件（data.exported）[/]")

    run_async(_run())


@main.command()
@click.option("--keep-days", default=30, help="备份保留天数")
@click.option("--restore-from", default=None, help="从指定备份恢复（危险操作）")
@click.option("--yes", is_flag=True, help="恢复操作确认")
def backup(keep_days: int, restore_from: str, yes: bool):
    """数据备份（SQLite 在线 checkpoint + 报告/事件流打包）"""
    from .engine.backup import BackupManager

    if restore_from:
        result = BackupManager().restore(restore_from, confirm=yes)
    else:
        result = BackupManager().run()
        if result["ok"]:
            removed = BackupManager().prune(keep_days=keep_days)
            if removed:
                console.print(f"[dim]已清理 {removed} 个过期备份[/]")

    if result["ok"]:
        console.print(f"[green]✓ {result.get('restored_from', result.get('backup_dir'))}[/]")
        if result.get("previous_data_snapshot"):
            console.print(f"原数据快照: {result['previous_data_snapshot']}")
        if result.get("note"):
            console.print(f"[yellow]{result['note']}[/]")
    else:
        console.print(f"[red]{result['detail']}[/]")
        sys.exit(1)


@main.group()
def embed():
    """嵌入交付（只读面板 + 签名令牌）"""
    pass


@embed.command("token")
@click.option("--workspace", "-w", required=True, help="工作区ID")
@click.option("--panel", "-p", required=True,
              help="面板：cockpit:traffic / metric:gsc_clicks")
@click.option("--hours", default=720, help="有效期（小时）")
@click.option("--company", default="", help="白标公司名")
def embed_token(workspace: str, panel: str, hours: int, company: str):
    """签发嵌入令牌（贴到 iframe src 即可）"""
    from .engine.embed import mint
    branding = {"company": company} if company else {}
    token = mint(workspace, panel, hours=hours, branding=branding)
    url = (f"/inflow/console/embed?token={token}")
    console.print("[green]嵌入代码（含缓存绕过，贴到交付页面）：[/]")
    console.print(
        f'<iframe id="ifEmbed" width="100%" height="420" style="border:0" '
        f'title="Insight Flow 面板" loading="lazy"></iframe>\n'
        f'<script>(function(){{var f=document.getElementById("ifEmbed");'
        f'f.src="{url}"+(f.src.indexOf("?")<0?"?":"&")+"_t="+Date.now();}})();</script>')


@main.group("rollup")
def rollup_group():
    """预聚合（metric_daily 汇总表）：长窗口查询提速"""
    pass


@rollup_group.command("build")
@click.option("--workspace", "-w", default="", help="工作区ID（空=全部）")
@click.option("--days", default=400, help="回溯天数")
def rollup_build(workspace: str, days: int):
    """构建/刷新日汇总（幂等，可定时执行）"""
    from .core.rollup import rollup, rollup_stats
    res = run_async(rollup(workspace, days=days))
    stats = run_async(rollup_stats(workspace))
    console.print(f"[green]OK 汇总完成[/] 写入 {res['rows']} 行 · "
                  f"覆盖 {stats['first_day']} ~ {stats['last_day']} · "
                  f"{stats['metrics']} 个指标")


@rollup_group.command("status")
@click.option("--workspace", "-w", default="")
def rollup_status(workspace: str):
    """查看汇总表状态"""
    from .core.rollup import rollup_stats
    console.print(run_async(rollup_stats(workspace)))


@main.command("snapshot")
@click.option("--workspace", "-w", required=True)
@click.option("--panel", "-p", default="board:traffic",
              help="board:<驾驶舱> 或 metric:<指标>")
@click.option("--days", default=30)
@click.option("--no-pdf", is_flag=True, help="只出 HTML（不调用 Chrome）")
@click.option("--push", is_flag=True, help="生成后按通知渠道推送链接")
def snapshot_cmd(workspace: str, panel: str, days: int, no_pdf: bool, push: bool):
    """生成看板快照（自包含 HTML，可直接打印 PDF）"""
    if push:
        from .engine.snapshot import push_snapshot
        res = run_async(push_snapshot(workspace, panel, days=days))
    else:
        from .engine.snapshot import create_snapshot
        res = run_async(create_snapshot(workspace, panel, days=days,
                                        make_pdf=not no_pdf))
    console.print(f"[green]快照已生成[/] {res.get('html_path')} "
                  f"（{res.get('size_kb')}KB）")
    if res.get("pdf_path"):
        console.print(f"  PDF: {res['pdf_path']}（{res.get('pdf_kb')}KB）")
    elif res.get("pdf_skipped"):
        console.print(f"  [yellow]PDF 跳过：{res['pdf_skipped']}[/]")
    if push:
        console.print(f"  推送：{res.get('notified')}")


@main.command("bench")
@click.option("--monitors", default=1000, help="监控任务数（默认 1000，对齐性能基线）")
@click.option("--insights", default=100000, help="洞察行数（默认 10 万）")
@click.option("--metrics", default=200000, help="指标行数（默认 20 万）")
@click.option("--driver", default="sqlite", type=click.Choice(["sqlite", "mysql"]))
@click.option("--keep", is_flag=True, help="保留临时库（SQLite）以便复查")
def bench_cmd(monitors, insights, metrics, driver, keep):
    """性能基准：合成数据 + 关键查询 P50/P95（不影响生产库）"""
    from .engine.bench import run_bench
    console.print(f"[cyan]造数中[/] monitors={monitors} insights={insights} "
                  f"metrics={metrics} driver={driver}")
    rep = run_async(run_bench(monitors=monitors, insights=insights, metrics=metrics,
                              driver=driver, keep=keep))
    console.print(f"[green]写入[/] {rep['dataset']} · {rep['write']['seconds']}s "
                  f"（{rep['write']['rows_per_sec']:,} 行/秒）"
                  + (f" · 库大小 {rep['db_file_mb']}MB" if rep.get("db_file_mb") else ""))
    table = Table(title=f"查询延迟（{rep['driver']}）")
    table.add_column("查询")
    table.add_column("n", justify="right")
    table.add_column("P50 (ms)", justify="right")
    table.add_column("P95 (ms)", justify="right")
    table.add_column("max (ms)", justify="right")
    for name, st in rep["queries"].items():
        table.add_row(name, str(st["n"]), str(st["p50_ms"]), str(st["p95_ms"]),
                      str(st["max_ms"]))
    console.print(table)


@main.group("verify")
def verify_group():
    """动作验证（14 天窗口）：手动触发评估与基线查看"""
    pass


@verify_group.command("run")
@click.option("--workspace", "-w", required=True)
def verify_run(workspace: str):
    """立即评估到期动作（写入结构化验证结论）"""
    from .actions.feedback_tracker import FeedbackTracker, default_metrics_provider
    tracker = FeedbackTracker(workspace)
    results = run_async(tracker.evaluate_due_actions(
        metrics_provider=default_metrics_provider(workspace)))
    done = [r for r in results if r.get("evaluated")]
    console.print(f"[green]到期 {len(results)} 个 · 完成 {len(done)} 个[/]")
    for r in results:
        if not r.get("evaluated"):
            console.print(f"  - {r['action_id'][:8]} 跳过：{r.get('error', '')[:50]}")


@verify_group.command("summary")
@click.option("--workspace", "-w", required=True)
def verify_summary(workspace: str):
    """结构化验证结论汇总"""
    from .core.store import get_store

    async def _run():
        store = await get_store()
        return await store.verification_summary(workspace)
    s = run_async(_run())
    console.print(f"结论 {s['total']} · 有效 {s['effective']} · 显著 {s['significant']}")
    for k, v in s["by_action_type"].items():
        console.print(f"  {k:<26} 总 {v['total']} 有效 {v['effective']} "
                      f"平均效应 {v['avg_effect_pct']}")


@main.group("evolution")
def evolution_group():
    """自进化：提案 / 评测门 / 生效 / 回滚（灰盒自整定）"""
    pass


@evolution_group.command("list")
@click.option("--workspace", "-w", required=True)
def evolution_list(workspace: str):
    """查看进化账本"""
    from .core.store import get_store

    async def _run():
        store = await get_store()
        return await store.list_evolution_runs(workspace)
    runs = run_async(_run())
    for r in runs:
        console.print(f"  {r['id'][:8]} {r['kind']:<16} {r['status']:<11} "
                      f"{r['target'][:12]:<14} {str(r.get('after'))[:60]}")


@evolution_group.command("propose")
@click.option("--workspace", "-w", required=True)
@click.option("--no-structures", is_flag=True, help="只提参数（阈值/权重），不提结构")
def evolution_propose(workspace: str, no_structures: bool):
    """生成提案（参数：阈值/权重；结构：规则/监控/看板。都不自动生效）"""
    from .engine.evolution import propose_all
    out = run_async(propose_all(workspace, structures=not no_structures))
    console.print(f"[green]阈值 {out['threshold_tunes']} · 权重 {out['model_weights']} · "
                  f"规则 {out['structure_dsl']} · 监控 {out['structure_monitor']} · "
                  f"看板 {out['structure_board']}[/]")
    if out["runs"]:
        console.print(f"  账本 id：{', '.join(r[:8] for r in out['runs'])}"
                      f"（`insflow evolution apply <id> -w {workspace}` 生效）")


@evolution_group.command("apply")
@click.argument("run_id")
@click.option("--workspace", "-w", required=True)
@click.option("--force", is_flag=True, help="跳过评测门（需谨慎）")
def evolution_apply(run_id: str, workspace: str, force: bool):
    """生效提案（先过评测门）"""
    from .engine.evolution import EvolutionError, apply_run
    try:
        res = run_async(apply_run(workspace, run_id, force=force))
    except EvolutionError as e:
        console.print(f"[red]拒绝：{e}[/]")
        raise SystemExit(1)
    console.print(f"[green]已生效[/] {res['applied']}")


@evolution_group.command("rollback")
@click.argument("run_id")
@click.option("--workspace", "-w", required=True)
def evolution_rollback(run_id: str, workspace: str):
    """回滚已生效提案"""
    from .engine.evolution import EvolutionError, rollback_run
    try:
        res = run_async(rollback_run(workspace, run_id))
    except EvolutionError as e:
        console.print(f"[red]失败：{e}[/]")
        raise SystemExit(1)
    console.print(f"[green]已回滚[/] 恢复 {res['restored']}")


@evolution_group.command("review")
@click.option("--workspace", "-w", required=True)
@click.option("--days", default=0, type=int, help="复盘窗口天数（默认 14）")
def evolution_review(workspace: str, days: int):
    """复盘生效满 N 天的提案，把效果写回账本"""
    from .engine.evolution import REVIEW_AFTER_DAYS, review_due
    out = run_async(review_due(workspace, days=days or REVIEW_AFTER_DAYS))
    if not out["reviewed"]:
        console.print("[yellow]没有到期待复盘的提案[/]（生效未满窗口或已复盘）")
        return
    console.print(f"[green]复盘 {out['reviewed']} 条[/]")
    for r in out["runs"]:
        console.print(f"  {r['run_id'][:8]} {r['kind']:<18} {r['verdict']}")


@evolution_group.command("accuracy")
@click.option("--workspace", "-w", required=True)
def evolution_accuracy(workspace: str):
    """提案准确率（improved / 已判定；样本不足的不计入分母）"""
    from .engine.evolution import accuracy
    a = run_async(accuracy(workspace))
    if not a["reviewed"]:
        console.print("[yellow]还没有复盘记录[/]（先 `insflow evolution review`）")
        return
    rate = f"{a['accuracy']:.0%}" if a["accuracy"] is not None else "—（无已判定样本）"
    console.print(f"[bold]准确率 {rate}[/] · 已复盘 {a['reviewed']} · 已判定 {a['judged']}")
    console.print(f"  {a['tally']}")
    for kind, row in a["by_kind"].items():
        console.print(f"  {kind:<18} {row}")


@main.command("behavior")
@click.option("--workspace", "-w", required=True)
def behavior_cmd(workspace: str):
    """行为信号与默认布局建议（本地统计，不上传）"""
    from .engine.behavior import suggest_defaults, summary
    sm = run_async(summary(workspace))
    sg = run_async(suggest_defaults(workspace))
    console.print(f"[bold]行为事件 {sm['total_events']} 次[/] · {sg['basis']}")
    for kind, rows in sm["by_kind"].items():
        if rows:
            console.print(f"  {kind}: " + ", ".join(
                f"{r['key']}×{r['hits']}" for r in rows[:5]))
    if sg["enough"]:
        console.print(f"  建议默认舱：{sg['default_cockpit']} · "
                      f"常用面板：{', '.join(sg['panel_ids']) or '—'}")


@main.command("playbook")
@click.option("--workspace", "-w", required=True)
@click.option("--mine", is_flag=True, help="从验证结论重新挖掘")
def playbook_cmd(workspace: str, mine: bool):
    """配方（验证有效的经验 → 可复用模板草案）"""
    from .engine.playbooks import list_playbooks
    from .engine.playbooks import mine as mine_pb
    if mine:
        drafts = run_async(mine_pb(workspace))
        console.print(f"[green]挖掘到 {len(drafts)} 个配方[/]")
        for d in drafts:
            console.print(f"  - {d['id']}：样本 {d['samples']} · "
                          f"有效率 {d['effective_rate']} · 平均效应 {d['avg_effect_pct']}")
    for pb in list_playbooks(workspace):
        console.print(f"  {pb['id']:<40} {pb['name'][:30]}")


@main.group("action")
def action_group():
    """动作与死信（跨系统派发可靠性）"""
    pass


@action_group.command("pending")
@click.option("--workspace", "-w", required=True, help="工作区ID")
def action_pending(workspace: str):
    """列出待审批动作（Agent/MCP 起草，批准后才派发）"""
    from .engine.proposals import list_pending

    items = run_async(list_pending(workspace))
    console.print(f"[green]{len(items)} 条待审批[/]")
    for p in items:
        console.print(f"  - {p['action_id']} [bold]{p['action_type']}[/] "
                      f"({p.get('proposed_by') or 'agent'})")
        console.print(f"    洞察：{(p.get('title') or p.get('insight_title') or '')[:60]}")
        if p.get("rationale"):
            console.print(f"    [dim]理由：{p['rationale'][:80]}[/]")
        console.print(f"    [dim]批准：insflow action approve {p['action_id']} -w {workspace}"
                      f" ｜ 拒绝：insflow action reject {p['action_id']} -w {workspace}[/]")


@action_group.command("approve")
@click.argument("action_id")
@click.option("--workspace", "-w", required=True, help="工作区ID")
@click.option("--actor", default="cli", help="操作人（审计）")
def action_approve(action_id: str, workspace: str, actor: str):
    """批准并派发（+ 基线 + 14 天验证窗口）"""
    from .engine.proposals import approve_action

    result = run_async(approve_action(workspace, action_id, actor=actor))
    if result.get("ok"):
        console.print(f"[green]已批准派发[/] 验证窗口至 {result.get('verify_window_until') or '—'}")
    else:
        console.print(f"[red]批准失败：{result.get('error')}[/]")
        sys.exit(1)


@action_group.command("reject")
@click.argument("action_id")
@click.option("--workspace", "-w", required=True, help="工作区ID")
@click.option("--reason", default="", help="拒绝原因")
@click.option("--actor", default="cli", help="操作人（审计）")
def action_reject(action_id: str, workspace: str, reason: str, actor: str):
    """拒绝提案（pending → cancelled）"""
    from .engine.proposals import reject_action

    result = run_async(reject_action(workspace, action_id, actor=actor, reason=reason))
    console.print("[green]已拒绝[/]" if result.get("ok") else "[red]拒绝失败[/]")


@action_group.command("dead-letters")
@click.option("--workspace", "-w", required=True)
@click.option("--all", "show_all", is_flag=True, help="含已重放")
def action_dead_letters(workspace: str, show_all: bool):
    """列出死信（失败动作的可重放载荷）"""
    from .core.store import get_store

    async def _run():
        store = await get_store()
        return await store.list_dead_letters(workspace, only_pending=not show_all)
    letters = run_async(_run())
    console.print(f"[green]{len(letters)} 条[/]")
    for letter in letters:
        console.print(f"  - {letter['id'][:8]} {letter['action_type']:<24} "
                      f"{letter['error'][:50]}"
                      + (f"（已重放：{letter['replay_result'][:40]}）"
                         if letter.get("replayed_at") else ""))


@action_group.command("replay")
@click.option("--workspace", "-w", required=True)
@click.option("--id", "letter_id", default="", help="只重放指定死信")
def action_replay(workspace: str, letter_id: str):
    """重放死信（重新派发；成功则动作回到 dispatched）"""
    from .actions.router import ActionContext, get_action_router
    from .core.store import get_store

    async def _run():
        store = await get_store()
        letters = await store.list_dead_letters(workspace, limit=50)
        if letter_id:
            letters = [x for x in letters if x["id"] == letter_id]
        router = get_action_router()
        ok = 0
        for letter in letters:
            row = await store.get_action(letter["action_id"]) if letter["action_id"] else None
            if not row:
                await store.mark_dead_letter_replayed(workspace, letter["id"], "动作不存在")
                continue
            res = await router.dispatch({
                "action_type": row.action_type, "target_ref": row.target_ref,
                "params_json": row.params_json or {}, "title": row.title,
                "summary": row.summary, "severity": str(row.severity),
            }, ActionContext(workspace_id=workspace, insight_id=row.insight_id))
            await store.mark_dead_letter_replayed(
                workspace, letter["id"],
                json.dumps(res, ensure_ascii=False, default=str)[:1000])
            if res.get("ok"):
                await store.update_action_state(row.id, "dispatched",
                                                result_json={"replay": True, **res})
                ok += 1
            console.print(f"  {'OK ' if res.get('ok') else 'FAIL'} {row.action_type} "
                          f"{str(res)[:70]}")
        return len(letters), ok
    total, ok = run_async(_run())
    console.print(f"[green]重放 {total} 条，成功 {ok} 条[/]")


@main.group("skill")
def skill_group():
    """Agent Skill 市场（扩展问答与分析方法）"""
    pass


@skill_group.command("list")
@click.option("--workspace", "-w", default="default")
def skill_list(workspace: str):
    """列出可安装的 Skill 包与已加载 Skill"""
    from .agent.skills_host import SkillsHost
    from .engine.skill_market import SkillMarket
    market = SkillMarket(workspace).scan()
    console.print(f"[green]市场 {len(market)} 个[/]")
    for m in market:
        console.print(f"  - {m.get('name')}: {str(m.get('description'))[:60]}")
    installed = SkillsHost().list_skills()
    console.print(f"[green]已加载 {len(installed)} 个[/]：" +
                  "、".join(i.get("name", "") for i in installed[:10]))


@skill_group.command("install")
@click.argument("path")
@click.option("--workspace", "-w", default="default")
def skill_install(path: str, workspace: str):
    """安装 Skill 包（目录路径；安装后 Agent 热加载）"""
    from .engine.skill_market import SkillMarket
    try:
        res = run_async(SkillMarket(workspace).install(path))
    except Exception as e:
        console.print(f"[red]安装失败：{type(e).__name__}: {e}[/]")
        raise SystemExit(1)
    console.print(f"[green]已安装[/] {res.get('name')} → {res.get('path')}")


@main.command("journey")
@click.option("--framework", "-f", default="see-think-do-care")
def journey_cmd(framework: str):
    """查看旅程框架定义（阶段/信号/指标/内容类型）"""
    from .engine.journey import get_framework
    fw = get_framework(framework)
    console.print(f"[green]{fw['name']}[/]（{len(fw['stages'])} 阶段）")
    for st in fw["stages"]:
        console.print(f"  - {st['name']}：{st['definition'][:46]}")


@main.group("privacy")
def privacy_group():
    """隐私合规：数据主体导出/删除 + 留存清理"""
    pass


@privacy_group.command("export")
@click.option("--workspace", "-w", required=True)
@click.option("--email", "-e", required=True)
@click.option("--out", default="", help="输出文件（默认打印到终端）")
def privacy_export(workspace: str, email: str, out: str):
    """DSAR：导出该主体的全部数据"""
    from .engine.privacy import export_subject
    data = run_async(export_subject(workspace, email))
    text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    if out:
        # CLI 单次导出写文件：同步即可（命令内无并发；见 docs/11 工程规范）
        pathlib.Path(out).write_text(text, encoding="utf-8")  # noqa: ASYNC230
        console.print(f"[green]已导出[/] {out}（含个人数据，请安全交付）")
    else:
        console.print(text[:4000])


@privacy_group.command("erase")
@click.option("--workspace", "-w", required=True)
@click.option("--email", "-e", required=True)
@click.option("--purge", is_flag=True, help="物理删除（默认匿名化）")
@click.option("--yes", is_flag=True, help="跳过 dry-run 直接执行")
def privacy_erase(workspace: str, email: str, purge: bool, yes: bool):
    """RTBF：删除/匿名化主体数据"""
    from .engine.privacy import erase_subject
    plan = run_async(erase_subject(workspace, email, purge=purge, dry_run=not yes))
    console.print(json.dumps(plan, ensure_ascii=False, indent=2, default=str)[:3000])


@privacy_group.command("retention")
@click.option("--workspace", "-w", required=True)
@click.option("--day/--days", "days", default=None, help="覆盖保留天数（metrics）")
@click.option("--yes", is_flag=True, help="跳过 dry-run 直接执行")
def privacy_retention(workspace: str, days, yes: bool):
    """留存策略清理（默认 dry-run）"""
    from .engine.privacy import retention_sweep
    res = run_async(retention_sweep(workspace, dry_run=not yes,
                                    override={"metrics_days": days} if days else None))
    console.print(json.dumps(res, ensure_ascii=False, indent=2, default=str)[:3000])


@main.group("dq")
def dq_group():
    """数据质量 SLA：体检与断点续采"""
    pass


@dq_group.command("check")
@click.option("--workspace", "-w", required=True)
@click.option("--days", default=14)
def dq_check(workspace: str, days: int):
    """新鲜度/完整性体检"""
    from .engine.data_quality import check_workspace
    rep = run_async(check_workspace(workspace, window_days=days))
    s = rep["summary"]
    console.print(f"[green]指标 {s['metrics']}[/] 新鲜 {s['fresh']} · 延迟 {s['late']} · "
                  f"停更 {s['stale']} · 缺失 {s['missing']} · 有缺口 {s['with_gaps']}")
    for i in rep["items"]:
        if i["status"] != "fresh":
            console.print(f"  - {i['metric']:<28} {i['status']:<8} {i['age_hours']}h "
                          f"缺口 {i['gap_count']} · {i['recoverable']}")


@dq_group.command("backfill")
@click.option("--workspace", "-w", required=True)
@click.option("--days", default=7)
@click.option("--dry-run", is_flag=True)
def dq_backfill(workspace: str, days: int, dry_run: bool):
    """断点续采：重跑有缺口指标对应的监控"""
    from .engine.data_quality import backfill
    res = run_async(backfill(workspace, days=days, dry_run=dry_run))
    console.print(f"计划 {len(res['planned'])} · 执行 {len(res['ran'])} · 跳过 {len(res['skipped'])}"
                  + ("（dry-run）" if res["dry_run"] else ""))


@main.command("attribution")
@click.option("--workspace", "-w", required=True)
@click.option("--method", default="linear",
              type=click.Choice(["last_click", "first_click", "linear", "time_decay",
                                 "markov"]))
@click.option("--days", default=30)
def attribution_cmd(workspace: str, method: str, days: int):
    """渠道归因（多触点）"""
    from .engine.attribution import channel_credit
    res = run_async(channel_credit(workspace, days=days, method=method))
    console.print(f"方法 {res['method']} · 路径 {res['paths_used']}"
                  + ("[yellow]（已降级为占比）[/]" if res["degraded"] else ""))
    for c in res["channels"]:
        console.print(f"  {c['channel']:<14} {c['share']}")


@main.command("lift")
@click.option("--workspace", "-w", required=True)
@click.option("--days", default=30)
def lift_cmd(workspace: str, days: int):
    """动作增量（前后对比 + 自助法区间）"""
    from .engine.attribution import lift_summary
    res = run_async(lift_summary(workspace, days=days))
    console.print(f"可评估 {res['total']} · 显著 {res['significant']} · "
                  f"数据不足 {res['insufficient_data']}")
    for a in res["actions"]:
        if a.get("insufficient_data"):
            continue
        console.print(f"  {a['action_type']:<24} {a['metric']:<20} "
                      f"{a.get('lift_pct') and format(a['lift_pct'], '+.1%')} "
                      f"CI {a['ci95']} {'显著' if a['significant'] else '不显著'}")


@main.command("narrate")
@click.option("--workspace", "-w", required=True)
@click.option("--metric", "-m", required=True)
@click.option("--days", default=30)
@click.option("--polish", is_flag=True, help="有 LLM 时润色（只用给定数字）")
def narrate_cmd(workspace: str, metric: str, days: int, polish: bool):
    """自动叙事：结论/证据/数据质量/建议"""
    from .engine.narrative import narrate
    res = run_async(narrate(workspace, metric, days=days, polish=polish))
    console.print(res["markdown"])


@main.group("alerts")
def alerts_group():
    """阈值告警规则：评估与升级"""
    pass


@alerts_group.command("check")
@click.option("--workspace", "-w", required=True)
def alerts_check(workspace: str):
    """立即评估规则（命中生成洞察并按路由通知）"""
    from .engine.alerts import evaluate_workspace, sweep_escalations
    fired = run_async(evaluate_workspace(workspace))
    esc = run_async(sweep_escalations(workspace))
    console.print(f"命中 {len(fired)} 条 · 升级 {len(esc)} 条")
    for f in fired:
        console.print(f"  - {f.get('name') or f.get('rule_id')}: "
                      f"{f.get('metric')} = {f.get('value')} "
                      f"(阈值 {f.get('op')} {f.get('threshold')})")


@main.command("estimate")
@click.option("--workspace", "-w", required=True)
@click.option("--domains", "-d", required=True, help="逗号分隔的域名")
@click.option("--days", default=30)
def estimate_cmd(workspace: str, domains: str, days: int):
    """域名相对流量指数（方法/置信度透明，不做绝对承诺）"""
    from .engine.traffic_estimate import estimate_many
    for e in run_async(estimate_many(workspace, [d for d in domains.split(",") if d], days)):
        console.print(f"{e['domain']:<24} 指数 {e['index']:<6} 区间 {e['range']} "
                      f"置信 {e['confidence']} 覆盖 {e['coverage']}")


@main.command("sql")
@click.option("--workspace", "-w", required=True)
@click.argument("query")
def sql_cmd(workspace: str, query: str):
    """只读 SQL 沙箱（白名单表 + 租户隔离）"""
    from .engine.explore_sql import SqlError
    from .engine.explore_sql import run as sql_run
    try:
        res = run_async(sql_run(workspace, query))
    except SqlError as e:
        console.print(f"[red]拒绝：{e}[/]")
        raise SystemExit(1)
    console.print(f"{res['count']} 行（截断 {res['truncated']}） · 列 {res['columns']}")
    for row in res["rows"][:20]:
        console.print("  " + " | ".join(str(v) for v in row))


@main.group()
def demo():
    """演示数据（供查看驾驶舱效果）"""
    pass


@demo.command("seed")
@click.option("--workspace", "-w", required=True, help="工作区ID")
@click.option("--days", default=30, help="演示时间跨度（天）")
def demo_seed(workspace: str, days: int):
    """生成演示数据（覆盖 9 个驾驶舱）"""
    from .engine.demo import DemoSeeder

    async def _seed():
        from .core.store import get_store
        store = await get_store()
        if not await store.get_workspace(workspace):
            console.print(f"[red]Workspace 不存在: {workspace}[/]")
            sys.exit(1)
        result = await DemoSeeder(workspace, days).seed()
        console.print(f"[green]✓ 演示数据已生成[/] 洞察 {result['insights']} 条 · "
                      f"动作 {result['actions']} 个 · 竞品 {result['competitors']} 个 · "
                      f"报告 {result['reports']} 份")

    run_async(_seed())


@demo.command("clear")
@click.option("--workspace", "-w", required=True, help="工作区ID")
def demo_clear(workspace: str):
    """清理演示数据（仅删带 demo 标记的行）"""
    from .engine.demo import DemoSeeder

    async def _clear():
        counts = await DemoSeeder(workspace).clear()
        console.print("[green]✓ 已清理[/] " +
                      " · ".join(f"{k} {v}" for k, v in counts.items() if v))

    run_async(_clear())


@main.group()
def db():
    """数据库（SQLite / MySQL 双驱动）"""
    pass


@db.command("status")
def db_status():
    """当前驱动与容量（运维观察）"""
    from .core.db import resolve_driver

    async def _status():
        from .core.store import get_store
        store = await get_store()
        h = await store.health()
        console.print(f"驱动: [bold]{resolve_driver()}[/]")
        console.print(f"大小: {h['db_size_bytes'] / 1048576:.1f} MB"
                      + (f" | WAL {h['wal_size_bytes'] / 1048576:.1f} MB"
                         if h.get("wal_size_bytes") else ""))
        console.print(f"查询: {h['queries']} 次 · 慢查询 {h['slow_queries']} · p95 {h['p95_ms']}ms")
        rows = {k: v for k, v in h["row_counts"].items() if v}
        if rows:
            table = Table(title="行数")
            table.add_column("表", style="cyan")
            table.add_column("行数", style="green")
            for k, v in rows.items():
                table.add_row(k, f"{v:,}")
            console.print(table)

    run_async(_status())


@db.command("migrate")
def db_migrate():
    """按当前驱动建表/迁移（MySQL 需先建库建用户）"""
    from .core.db import mysql_config_from_env, resolve_driver

    async def _migrate():
        from .core.store import Store
        driver = resolve_driver()
        if driver == "mysql":
            cfg = mysql_config_from_env()
            console.print(f"[bold]MySQL[/] {cfg['user']}@{cfg['host']}:{cfg['port']}/{cfg['dbname']}")
        else:
            console.print("[bold]SQLite[/]（内嵌）")
        store = Store(driver=driver)
        await store.connect()
        await store.migrate()
        h = await store.health()
        console.print(f"[green]✓ 迁移完成[/] 驱动={h['driver']} "
                      f"表 {sum(1 for v in h['row_counts'].values() if v is not None)} 张")
        await store.close()

    run_async(_migrate())


@main.command()
@click.option("--workspace", "-w", default="default", help="工作区ID")
def doctor(workspace: str):
    """首次运行体检（环境/凭据/插件/调度/磁盘五项，含修复建议）"""
    from .engine.doctor import Doctor

    result = Doctor(workspace).run_all()
    for section, checks in result["sections"].items():
        console.print(f"\n[bold]{section}[/]")
        for c in checks:
            console.print(f"  {c.icon} {c.name}：{c.detail}")
            if c.fix:
                console.print(f"     [dim]→ {c.fix}[/]")

    s = result["summary"]
    verdict_color = {"healthy": "green", "degraded": "yellow", "unhealthy": "red"}[s["verdict"]]
    console.print(f"\n[bold {verdict_color}]总体：{s['verdict']}[/]"
                  f"（{s['total']} 项检查，{s['failed']} 失败 / {s['warned']} 警告）")
    if s["failed"]:
        sys.exit(1)


@main.command()
@click.option("--workspace", "-w", default=None, help="工作区ID（不填则新建）")
@click.option("--name", default="InsFlow Demo", help="新工作区名称")
@click.option("--pack", "-p", default="saas-growth", help="行业模板包")
@click.option("--base-url", default="http://127.0.0.1:8400", help="服务地址（向导链接用）")
def setup(name: str, workspace: str, pack: str, base_url: str):
    """引导式初始化：体检 → 建 workspace → 应用行业模板 → 输出接入向导链接"""
    async def _setup():
        # 1. 体检（非阻塞：警告继续，失败才停）
        from .engine.doctor import Doctor
        report = Doctor(workspace or "default").run_all()
        failed = report["summary"]["failed"]
        if failed and not workspace:
            console.print("[red]体检未通过，先处理修复建议再运行 setup[/]")
            sys.exit(1)
        console.print(f"[dim]体检：{report['summary']['verdict']}（{failed} 失败 / {report['summary']['warned']} 警告）[/]")

        from .core.store import get_store
        store = await get_store()

        # 2. 工作区（复用或新建）
        if workspace:
            ws = await store.get_workspace(workspace)
            if not ws:
                console.print(f"[red]Workspace 不存在: {workspace}[/]")
                sys.exit(1)
        else:
            from .core.entities import Workspace
            ws = await store.create_workspace(Workspace(name=name))
            console.print(f"[green]✓ 工作区: {ws.id}（{ws.name}）[/]")

        # 3. 应用行业模板
        from .engine.template_pack import TemplateRegistry
        reg = TemplateRegistry()
        pack_obj = reg.get(pack)
        if pack_obj:
            applied = await pack_obj.apply(ws.id)
            console.print(f"[green]✓ 模板: {pack_obj.name}[/]"
                          f"（监控 {len(applied['monitors_created'])} 新建 / {len(applied['monitors_skipped'])} 跳过，"
                          f"DSL {len(applied['dsl_rules_registered'])} 条）")

        # 4. 输出下一步
        console.print("\n[bold]下一步：[/]")
        console.print(f"  1. 接入向导: {base_url}/console/onboarding?workspace_id={ws.id}")
        console.print(f"  2. 触发首诊: POST {base_url}/api/v1/diagnosis/run")
        console.print(f"  3. 体检复查: insflow doctor -w {ws.id}")
        return ws.id

    run_async(_setup())


@main.group()
def template():
    """行业模板包"""
    pass


@template.command("export")
@click.option("--workspace", "-w", required=True)
@click.option("--out", "-o", required=True, help="输出 JSON 路径")
@click.option("--id", "template_id", default="", help="模板包 id")
@click.option("--name", default="", help="模板包名称")
@click.option("--industry", default="", help="行业标签")
def template_export(workspace: str, out: str, template_id: str, name: str,
                    industry: str):
    """把工作区配置沉淀为行业模板包（脱敏，可给下一个客户直接用）"""
    from .engine.template_pack import export_from_workspace
    res = run_async(export_from_workspace(workspace, template_id=template_id,
                                          name=name, industry=industry))
    if res["errors"]:
        console.print(f"[red]校验未通过：{res['errors']}[/]")
        raise SystemExit(1)
    pathlib.Path(out).write_text(json.dumps(res["spec"], ensure_ascii=False, indent=2),
                                 encoding="utf-8")
    console.print(f"[green]已导出[/] {out}（监控 {res['monitors']} · "
                  f"DSL {res['dsl_rules']}；不含数据与凭据）")


@template.command("validate")
@click.argument("path")
def template_validate(path: str):
    """校验模板包 JSON（应用前先验证）"""
    from .engine.template_pack import validate_template
    spec = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    errors = validate_template(spec)
    if errors:
        console.print(f"[red]不通过：{errors}[/]")
        raise SystemExit(1)
    console.print("[green]模板包有效[/]")


@template.command("list")
def template_list():
    """列出可用的行业模板包"""
    from .engine.template_pack import TemplateRegistry
    items = TemplateRegistry().list()
    table = Table(title="行业模板包")
    table.add_column("ID", style="cyan")
    table.add_column("名称", style="green")
    table.add_column("监控项", style="magenta")
    table.add_column("DSL 规则", style="yellow")
    for it in items:
        table.add_row(it["id"], it["name"], str(it["monitors"]), str(it["dsl_rules"]))
    console.print(table)


@template.command("apply")
@click.argument("template_id")
@click.option("--workspace", "-w", required=True, help="工作区ID")
def template_apply(template_id: str, workspace: str):
    """应用行业模板包到工作区（幂等）"""
    from .engine.template_pack import TemplateRegistry

    async def _apply():
        pack = TemplateRegistry().get(template_id)
        if not pack:
            console.print(f"[red]模板不存在: {template_id}[/]")
            sys.exit(1)
        result = await pack.apply(workspace)
        console.print(f"[green]✓ {pack.name}[/]：监控 +{len(result['monitors_created'])}"
                      f"（跳过 {len(result['monitors_skipped'])}），"
                      f"DSL +{len(result['dsl_rules_registered'])}")

    run_async(_apply())


@main.command()
@click.option("--workspace", "-w", required=True, help="工作区ID")
@click.option("--month", default=None, help="账期（YYYY-MM，默认当月）")
def invoice(workspace: str, month: str):
    """生成月度对账单"""
    from .engine.invoice import InvoiceBuilder

    async def _build():
        result = await InvoiceBuilder(workspace).build(month=month)
        console.print(f"[green]对账单已生成 → {result['invoice_path']}[/]")

    run_async(_build())


@main.command()
@click.option("--workspace", "-w", default="default", help="工作区ID")
@click.option("--out", "-o", default=None, help="导出路径（默认 audit-<ws>-<时间>.csv）")
@click.option("--format", "-f", "fmt", default="csv", type=click.Choice(["csv", "json"]))
@click.option("--from", "date_from", default=None, help="起始日期 YYYY-MM-DD")
@click.option("--to", "date_to", default=None, help="结束日期 YYYY-MM-DD")
@click.option("--type", "event_type", default=None, help="事件类型前缀（如 action.）")
@click.option("--keyword", default=None, help="关键字过滤")
def audit(workspace: str, out: str, fmt: str, date_from: str, date_to: str,
          event_type: str, keyword: str):
    """审计导出（企业采购合规）"""
    from .engine.audit import AuditExporter

    if not out:
        import datetime as _dt
        out = f"audit-{workspace}-{_dt.datetime.now().strftime('%Y%m%d-%H%M')}.{fmt}"

    result = AuditExporter(workspace).run(
        out, fmt=fmt, date_from=date_from, date_to=date_to,
        event_type=event_type, keyword=keyword)
    console.print(f"[green]✓ 审计导出 {result['count']} 条 → {result['path']}[/]")


@main.command()
def init():
    """初始化 Insight Flow 数据目录"""
    data_dir = Path("data")
    data_dir.mkdir(exist_ok=True)
    (data_dir / "reports").mkdir(exist_ok=True)
    (data_dir / "snapshots").mkdir(exist_ok=True)
    (data_dir / "events").mkdir(exist_ok=True)
    console.print("[green]Initialized data directory[/]")


if __name__ == "__main__":
    main()


@main.group()
def subscribe():
    """洞察订阅推送"""
    pass


@subscribe.command("list")
@click.option("--workspace", "-w", required=True, help="工作区ID")
def subscribe_list(workspace: str):
    """列出订阅"""
    from .engine.subscriptions import SubscriptionService

    async def _list():
        subs = await SubscriptionService(workspace).list()
        if not subs:
            console.print("[yellow]暂无订阅 —— 用 insflow subscribe add 创建[/]")
            return
        table = Table(title="洞察订阅")
        table.add_column("ID", style="cyan")
        table.add_column("名称", style="green")
        table.add_column("渠道", style="magenta")
        table.add_column("模式", style="yellow")
        table.add_column("启用", style="blue")
        for s in subs:
            table.add_row(s["id"], s["name"], ",".join(s["channels"]),
                          s["mode"], "✓" if s["enabled"] else "✗")
        console.print(table)

    run_async(_list())


@subscribe.command("add")
@click.option("--workspace", "-w", required=True, help="工作区ID")
@click.option("--name", "-n", required=True, help="订阅名称")
@click.option("--feishu-url", default=None, help="飞书机器人 webhook")
@click.option("--webhook-url", default=None, help="通用 Webhook URL")
@click.option("--webhook-secret", default="", help="Webhook HMAC 密钥")
@click.option("--severity", default=None, help="严重度过滤（逗号分隔：critical,high）")
@click.option("--type-prefix", default=None, help="类型前缀过滤（逗号分隔）")
@click.option("--query", default=None, help="关键字过滤")
@click.option("--mode", default="immediate", type=click.Choice(["immediate", "daily"]))
def subscribe_add(workspace, name, feishu_url, webhook_url, webhook_secret,
                  severity, type_prefix, query, mode):
    """创建订阅（洞察创建即推 / 每日汇总）"""
    from .engine.subscriptions import SubscriptionError, SubscriptionService

    channels, target = [], {}
    if feishu_url:
        channels.append("feishu")
        target["feishu_url"] = feishu_url
    if webhook_url:
        channels.append("webhook")
        target["webhook_url"] = webhook_url
        target["webhook_secret"] = webhook_secret
    filters = {}
    if severity:
        filters["severity"] = [s.strip() for s in severity.split(",")]
    if type_prefix:
        filters["type_prefix"] = [s.strip() for s in type_prefix.split(",")]
    if query:
        filters["query"] = query

    async def _create():
        try:
            sub = await SubscriptionService(workspace).create(name, channels, target, filters, mode)
            console.print(f"[green]✓ 订阅已创建 {sub['id']}（{','.join(channels)} / {mode}）[/]")
        except SubscriptionError as e:
            console.print(f"[red]{e}[/]")
            sys.exit(1)

    run_async(_create())


@subscribe.command("rm")
@click.argument("subscription_id")
@click.option("--workspace", "-w", required=True, help="工作区ID")
def subscribe_rm(subscription_id: str, workspace: str):
    """删除订阅"""
    from .engine.subscriptions import SubscriptionService

    async def _rm():
        ok = await SubscriptionService(workspace).delete(subscription_id)
        console.print("[green]已删除[/]" if ok else "[red]订阅不存在[/]")

    run_async(_rm())
