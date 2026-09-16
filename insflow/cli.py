"""Insight Flow CLI 入口"""

import asyncio
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

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
@click.version_option(version="0.1.0", prog_name="insflow")
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
