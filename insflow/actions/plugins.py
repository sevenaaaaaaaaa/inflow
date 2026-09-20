"""动作适配器插件加载：plugins/actions/*/create_adapter() → ActionRouter

不过检的插件不注册（与采集调度器同一条铁律）。不覆盖内置 action_type。
"""

from __future__ import annotations

import importlib.util
import json
import logging
from pathlib import Path

from ..engine.marketplace import check_plugin
from .router import ActionAdapter, ActionRouter

log = logging.getLogger("insflow.actions")


def _actions_dir() -> Path:
    """每次读模块属性，不做 from-import 快照

    `from ..engine.marketplace import PLUGINS_DIR` 会把路径固化在导入那一刻：
    运行期改插件目录（多实例挂载、测试 monkeypatch）全部失效，且只在"本模块先被
    别的用例导入过"时才暴露——典型的顺序相关假绿。
    """
    from ..engine import marketplace
    return marketplace.PLUGINS_DIR / "actions"


def load_action_plugins(router: ActionRouter,
                        directory: Path | None = None) -> list[dict]:
    """扫描并注册动作插件。返回 [{plugin_id, action_type, ok, detail}]。"""
    base = directory or _actions_dir()
    reports: list[dict] = []
    if not base.exists():
        return reports
    occupied = set(router.list_types())
    for plugin_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        manifest_path = plugin_dir / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            reports.append({"plugin_id": plugin_dir.name, "ok": False,
                            "detail": f"manifest: {e}"})
            continue
        if manifest.get("type") != "action":
            continue
        report = check_plugin(plugin_dir)
        plugin_id = str(manifest.get("id") or plugin_dir.name)
        if not report["passed"]:
            reports.append({"plugin_id": plugin_id, "ok": False,
                            "detail": "; ".join(report["errors"])})
            continue
        adapter = _instantiate(plugin_dir, manifest)
        if adapter is None:
            reports.append({"plugin_id": plugin_id, "ok": False,
                            "detail": "create_adapter() 未返回 ActionAdapter"})
            continue
        action_type = adapter.action_type
        if action_type in occupied:
            reports.append({"plugin_id": plugin_id, "ok": False,
                            "action_type": action_type,
                            "detail": f"动作类型已被占用: {action_type}"})
            continue
        adapter.plugin_id = plugin_id  # type: ignore[attr-defined]
        adapter.plugin_name = str(manifest.get("name") or plugin_id)
        adapter.source = "plugin"  # type: ignore[attr-defined]
        router.register(adapter)
        occupied.add(action_type)
        reports.append({"plugin_id": plugin_id, "ok": True,
                        "action_type": action_type})
    return reports


def reload_action_plugins(router: ActionRouter | None = None,
                          directory: Path | None = None) -> list[dict]:
    """卸掉已注册的插件适配器再扫一遍（安装/卸载后热更新）。"""
    from .router import get_action_router
    target = router or get_action_router()
    for key in [k for k, a in target._adapters.items()
                if getattr(a, "source", "builtin") == "plugin"]:
        target._adapters.pop(key, None)
    return load_action_plugins(target, directory)


def _instantiate(plugin_dir: Path, manifest: dict) -> ActionAdapter | None:
    entry = plugin_dir / manifest.get("entry", "entry.py")
    if not entry.exists():
        return None
    spec = importlib.util.spec_from_file_location(
        f"insflow_action_{manifest.get('id', plugin_dir.name)}", str(entry))
    if spec is None or spec.loader is None:
        return None
    try:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception:
        log.exception("加载动作插件失败: %s", plugin_dir)
        return None
    factory = getattr(module, "create_adapter", None)
    if callable(factory):
        obj = factory()
        return obj if isinstance(obj, ActionAdapter) else None
    return None
