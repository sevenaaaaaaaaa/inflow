"""Insight Flow 插件注册表"""

import importlib.util
import json
from pathlib import Path

from .base import SourcePlugin


class Registry:
    """插件注册表"""

    def __init__(self):
        self._plugins: dict[str, SourcePlugin] = {}

    def register(self, plugin: SourcePlugin) -> None:
        """注册插件"""
        self._plugins[plugin.id] = plugin

    def get(self, plugin_id: str) -> SourcePlugin | None:
        """获取插件"""
        return self._plugins.get(plugin_id)

    def list_plugins(self) -> list[dict]:
        """列出所有插件"""
        return [
            {
                "id": p.id,
                "name": p.name,
                "capabilities": p.capabilities,
            }
            for p in self._plugins.values()
        ]

    def load_from_directory(self, plugins_dir: Path) -> int:
        """从目录加载插件"""
        loaded = 0
        if not plugins_dir.exists():
            return loaded

        for plugin_dir in plugins_dir.iterdir():
            if not plugin_dir.is_dir():
                continue
            manifest_path = plugin_dir / "manifest.json"
            if not manifest_path.exists():
                continue

            try:
                manifest = json.loads(manifest_path.read_text())
                entry_file = plugin_dir / manifest.get("entry", "entry.py")
                if not entry_file.exists():
                    continue

                # 动态加载插件模块
                spec = importlib.util.spec_from_file_location(
                    f"insflow_plugin_{manifest['id']}", str(entry_file)
                )
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    if hasattr(module, "create_plugin"):
                        plugin = module.create_plugin()
                        self.register(plugin)
                        loaded += 1
            except Exception as e:
                print(f"Failed to load plugin from {plugin_dir}: {e}")

        return loaded


# 全局注册表实例
_registry: Registry | None = None


def get_registry() -> Registry:
    """获取全局注册表"""
    global _registry
    if _registry is None:
        _registry = Registry()
        # 自动加载内置插件
        builtin_plugins = Path(__file__).parent.parent.parent / "plugins" / "sources"
        _registry.load_from_directory(builtin_plugins)
    return _registry
