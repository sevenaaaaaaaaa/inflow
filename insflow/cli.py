"""Insight Flow CLI 入口"""

import asyncio
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


def run_async(coro):
    """运行异步函数（结束后关闭全局 store，避免 aiosqlite 线程阻塞退出）"""
    from .core.store import close_store
    try:
        return asyncio.get_event_loop().run_until_complete(coro)
    finally:
        try:
            asyncio.get_event_loop().run_until_complete(close_store())
        except Exception:
            pass


@click.group()
@click.version_option(version=__version__, prog_name="insflow")
def main():
    """Insight Flow - 增长情报与策略操作系统"""
    pass


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


@plugin.command("check")
@click.argument("plugin_path")
def plugin_check(plugin_path: str):
    """检查插件是否合规"""
    path = Path(plugin_path)
    if not path.exists():
        console.print(f"[red]Plugin path not found: {path}[/]")
        sys.exit(1)

    manifest_path = path / "manifest.json"
    if not manifest_path.exists():
        console.print(f"[red]manifest.json not found in {path}[/]")
        sys.exit(1)

    import json
    manifest = json.loads(manifest_path.read_text())

    errors = []
    warnings = []

    # 检查必填字段
    required_fields = ["id", "type", "name", "version", "entry"]
    for field in required_fields:
        if field not in manifest:
            errors.append(f"Missing required field: {field}")

    # 检查 type 枚举
    valid_types = ["source", "model", "action", "template"]
    if manifest.get("type") not in valid_types:
        errors.append(f"Invalid type: {manifest.get('type')}. Must be one of {valid_types}")

    # 检查 entry 文件是否存在
    entry_file = path / manifest.get("entry", "")
    if not entry_file.exists():
        errors.append(f"Entry file not found: {entry_file}")

    # 检查目录名是否等于 id
    if path.name != manifest.get("id"):
        warnings.append(f"Directory name '{path.name}' does not match plugin id '{manifest.get('id')}'")

    # 检查权限声明
    if "permissions" in manifest:
        for perm in manifest["permissions"]:
            if perm.startswith("credentials:"):
                # 检查是否有明文密钥
                config = manifest.get("config", {})
                for key, val in config.items():
                    if val.get("secret") and "default" in val:
                        errors.append(f"Secret config '{key}' must not have default value")

    # 输出结果
    if errors:
        console.print("[bold red]Plugin check FAILED[/]")
        for err in errors:
            console.print(f"  [red]✗[/] {err}")
        sys.exit(1)
    else:
        console.print("[bold green]Plugin check PASSED[/]")
        if warnings:
            for warn in warnings:
                console.print(f"  [yellow]⚠[/] {warn}")


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

    run_async(_ask())


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
            with open(path, "w", newline="", encoding="utf-8") as fh:
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
    from .engine.explore_sql import SqlError, run as sql_run
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
            console.print(f"[red]Workspace 不存在: {workspace}[/]"); sys.exit(1)
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
        channels.append("feishu"); target["feishu_url"] = feishu_url
    if webhook_url:
        channels.append("webhook"); target["webhook_url"] = webhook_url
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
            console.print(f"[red]{e}[/]"); sys.exit(1)

    run_async(_create())


@subscribe.command("rm")
@click.argument("subscription_id")
@click.option("--workspace", "-w", required=True, help="工作区ID")
def subscribe_rm(subscription_id: str, workspace: str):
    """删除订阅"""
    from .engine.subscriptions import SubscriptionService

    async def _rm():
        ok = await SubscriptionService(workspace).delete(subscription_id)
        console.print(f"[green]已删除[/]" if ok else "[red]订阅不存在[/]")

    run_async(_rm())
