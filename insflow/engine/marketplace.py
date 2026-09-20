"""Insight Flow 插件市场机制

本地目录优先 + 手动安装流程（registry 后置，M4 范围）：
- marketplace/ 本地插件仓库（按 type 分源/模型/动作目录存放）
- 安装 = plugin check 校验 → 拷贝到 plugins/{type}s/{id}/ → 热加载注册
- 安装/卸载全量写事件流（审计 PL-5）
"""

import json
import shutil
from pathlib import Path

from ..core.files import EventBus

ROOT_DIR = Path(__file__).parent.parent.parent
PLUGINS_DIR = ROOT_DIR / "plugins"
MARKETPLACE_DIR = ROOT_DIR / "marketplace"

TYPE_TO_DIR = {"source": "sources", "model": "models", "action": "actions", "template": "templates"}


def check_plugin(plugin_dir: Path) -> dict:
    """插件校验器（与 CLI `insflow plugin check` 同源逻辑）

    Returns:
        {"passed": bool, "errors": [...], "warnings": [...], "manifest": {...}|None}
    """
    errors: list[str] = []
    warnings: list[str] = []
    manifest = None

    if not plugin_dir.exists():
        return {"passed": False, "errors": [f"路径不存在: {plugin_dir}"],
                "warnings": warnings, "manifest": None}

    manifest_path = plugin_dir / "manifest.json"
    if not manifest_path.exists():
        return {"passed": False, "errors": ["manifest.json 不存在"],
                "warnings": warnings, "manifest": None}

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return {"passed": False, "errors": [f"manifest.json 解析失败: {e}"],
                "warnings": warnings, "manifest": None}

    # 1. 必填字段
    for field in ("id", "type", "name", "version", "entry"):
        if not manifest.get(field):
            errors.append(f"缺少必填字段: {field}")

    # 2. type 枚举
    ptype = manifest.get("type")
    if ptype not in TYPE_TO_DIR:
        errors.append(f"type 非法: {ptype}（合法: {list(TYPE_TO_DIR)}）")

    # 3. entry 存在 + 约定函数（source/model/action 各自的约定入口）
    entry = manifest.get("entry", "")
    entry_file = plugin_dir / entry
    if not entry_file.exists():
        errors.append(f"entry 文件不存在: {entry}")
    else:
        try:
            src = entry_file.read_text(encoding="utf-8")
        except OSError as e:
            src = ""
            errors.append(f"entry 不可读: {e}")
        if ptype == "source" and "def create_plugin" not in src and "create_plugin =" not in src:
            errors.append("source 插件缺少 create_plugin() 工厂函数")
        elif ptype == "model" and not any(
            token in src for token in ("def create_model", "create_model =", "def evaluate")
        ):
            errors.append("model 插件缺少 create_model() 或 evaluate()")
        elif ptype == "action" and not any(
            token in src for token in ("def create_adapter", "create_adapter =", "def execute")
        ):
            errors.append("action 插件缺少 create_adapter() 或 execute()")

    # 4. 目录名 = id
    if manifest.get("id") and plugin_dir.name != manifest["id"]:
        warnings.append(f"目录名 {plugin_dir.name} 与插件 id {manifest['id']} 不一致")

    # 5. 无明文密钥：secret 类配置不得带默认值；代码中不出现疑似密钥
    for key, cfg in (manifest.get("config") or {}).items():
        if isinstance(cfg, dict) and cfg.get("secret") and cfg.get("default"):
            errors.append(f"secret 配置 {key} 不得有 default 值")
    if entry_file.exists() and src:
        import re
        for pattern in (r"sk-[A-Za-z0-9]{20,}", r"AKIA[0-9A-Z]{16}", r"(?i)(api[_-]?key)\s*=\s*['\"][A-Za-z0-9]{16,}['\"]"):
            if re.search(pattern, src):
                errors.append("entry 代码中疑似存在明文密钥")
                break

    # 6. permissions 声明
    perms = manifest.get("permissions") or []
    if isinstance(perms, str):
        warnings.append("permissions 应为数组")
    elif "network" in perms and ptype != "source":
        warnings.append("非 source 插件声明 network 权限请确认必要性")

    return {"passed": not errors, "errors": errors, "warnings": warnings, "manifest": manifest}


class Marketplace:
    """本地插件市场"""

    def __init__(self, workspace_id: str = "default",
                 marketplace_dir: Path | None = None):
        self.workspace_id = workspace_id
        self.marketplace_dir = marketplace_dir or MARKETPLACE_DIR
        self.bus = EventBus(workspace_id)

    # ========== 浏览 ==========

    def scan(self) -> list[dict]:
        """列出本地市场可安装的插件（读 manifest + check 结果）"""
        if not self.marketplace_dir.exists():
            return []
        results = []
        for type_dir in self.marketplace_dir.iterdir():
            if not type_dir.is_dir():
                continue
            for plugin_dir in type_dir.iterdir():
                if not plugin_dir.is_dir():
                    continue
                report = check_plugin(plugin_dir)
                m = report["manifest"] or {}
                results.append({
                    "path": str(plugin_dir),
                    "type": ptype_dir(type_dir.name),
                    "id": (m or {}).get("id", plugin_dir.name),
                    "name": (m or {}).get("name", ""),
                    "version": (m or {}).get("version", ""),
                    "check_passed": report["passed"],
                    "check_errors": report["errors"],
                })
        return results

    # ========== 安装 / 卸载 ==========

    def install(self, plugin_path: str | Path) -> dict:
        """手动安装：check → 拷贝到 plugins/ → 事件流审计

        未过检的插件可强制安装（inspect 模式），但不会被调度器自动执行。
        """
        src = Path(plugin_path)
        report = check_plugin(src)
        if not report["manifest"]:
            return {"ok": False, "stage": "check", **report}

        ptype = report["manifest"]["type"]
        plugin_id = report["manifest"]["id"]
        dest = PLUGINS_DIR / TYPE_TO_DIR[ptype] / plugin_id
        if dest.exists():
            return {"ok": False, "stage": "exists",
                    "detail": f"插件已安装: {plugin_id}（先卸载再重装）"}

        shutil.copytree(src, dest)
        if ptype == "action":
            from ..actions.plugins import reload_action_plugins
            reload_action_plugins()
        self.bus.emit("plugin.installed", {
            "plugin_id": plugin_id, "type": ptype,
            "check_passed": report["passed"], "from": str(src),
        })
        return {
            "ok": True, "plugin_id": plugin_id, "type": ptype,
            "installed_to": str(dest),
            "warnings": report["warnings"],
            "auto_scheduled": report["passed"],  # 过检才会被调度器自动执行
        }

    def uninstall(self, plugin_id: str) -> dict:
        for type_dir in TYPE_TO_DIR.values():
            dest = PLUGINS_DIR / type_dir / plugin_id
            if dest.exists():
                shutil.rmtree(dest)
                if type_dir == "actions":
                    from ..actions.plugins import reload_action_plugins
                    reload_action_plugins()
                self.bus.emit("plugin.uninstalled", {"plugin_id": plugin_id, "type": type_dir})
                return {"ok": True, "removed": str(dest)}
        return {"ok": False, "detail": f"未找到插件: {plugin_id}"}

    def installed(self) -> list[dict]:
        """列出已安装插件"""
        out = []
        if not PLUGINS_DIR.exists():
            return out
        for type_dir, ptype in (("sources", "source"), ("models", "model"),
                                ("actions", "action")):
            base = PLUGINS_DIR / type_dir
            if not base.exists():
                continue
            for d in base.iterdir():
                if not d.is_dir():
                    continue
                m_file = d / "manifest.json"
                if not m_file.exists():
                    continue
                try:
                    m = json.loads(m_file.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    m = {}
                out.append({"id": m.get("id", d.name), "type": m.get("type", ptype),
                            "name": m.get("name", ""), "version": m.get("version", "")})
        return out


def ptype_dir(dir_name: str) -> str:
    """目录名 → 插件 type（sources→source）"""
    for t, d in TYPE_TO_DIR.items():
        if d == dir_name.rstrip("s"):
            return t
    return dir_name.rstrip("s")
