"""插件脚手架：`insflow plugin new <type> <id>`

生成能通过 `plugin check` 的最小插件（manifest + entry + README）。
source 进采集 Registry；action 过检后热加载进 ActionRouter。
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from .marketplace import PLUGINS_DIR, TYPE_TO_DIR, check_plugin

PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,47}$")


def class_prefix(plugin_id: str) -> str:
    return "".join(part.capitalize() for part in plugin_id.replace("_", "-").split("-"))


def validate_id(plugin_id: str) -> str | None:
    if not PLUGIN_ID_RE.match(plugin_id):
        return "id 须为小写字母开头、仅含 a-z / 0-9 / -，长度 2–48"
    return None


def default_dest(ptype: str, plugin_id: str, out_dir: Path | None = None) -> Path:
    parent = Path(out_dir) if out_dir else PLUGINS_DIR / TYPE_TO_DIR[ptype]
    return parent / plugin_id


def _manifest(ptype: str, plugin_id: str, name: str, entry: str) -> dict:
    body = {
        "id": plugin_id,
        "type": ptype,
        "name": name,
        "version": "0.1.0",
        "entry": entry,
        "permissions": [],
        "config": {},
    }
    if ptype == "source":
        body["permissions"] = ["network"]
        body["config"] = {
            "api_key": {"desc": "上游 API Key（若需要）", "required": False, "secret": True},
        }
        body["provides"] = {
            "metrics": [f"{plugin_id}_items"],
            "engines": ["custom"],
            "latency": "standard",
            "cost_hint": "0",
        }
    elif ptype == "action":
        body["permissions"] = ["network"]
    return body


def _source_entry(plugin_id: str) -> str:
    cls = class_prefix(plugin_id)
    return f'''"""自定义数据源插件（脚手架）。"""

from insflow.collectors.base import CollectContext, CollectResult, SourcePlugin


class {cls}Plugin(SourcePlugin):
    @property
    def id(self) -> str:
        return "{plugin_id}"

    @property
    def name(self) -> str:
        return "{cls} Source"

    async def collect(self, ctx: CollectContext) -> CollectResult:
        # 从 ctx.config 读凭据；返回归一化信封。cost 进配额账本。
        return CollectResult(
            source=self.id,
            kind="{plugin_id}_items",
            items=[],
            cost={{"units": 0.0, "currency": "USD"}},
        )


def create_plugin() -> SourcePlugin:
    return {cls}Plugin()
'''


def _model_entry(plugin_id: str) -> str:
    cls = class_prefix(plugin_id)
    return f'''"""自定义洞察模型（脚手架）。"""

from insflow.engine.router import InsightModel, ModelContext


class {cls}Model(InsightModel):
    @property
    def id(self) -> str:
        return "{plugin_id}"

    @property
    def name(self) -> str:
        return "{cls} Model"

    async def evaluate(self, ctx: ModelContext) -> list[dict]:
        # 返回 Insight 草稿列表；无 evidence/actions 的草稿会被质量门拦截。
        return []


def create_model() -> InsightModel:
    return {cls}Model()
'''


def _action_entry(plugin_id: str) -> str:
    cls = class_prefix(plugin_id)
    return f'''"""自定义动作适配器（脚手架）。"""

from insflow.actions.router import ActionAdapter, ActionContext, ActionResult


class {cls}Adapter(ActionAdapter):
    @property
    def action_type(self) -> str:
        return "custom.{plugin_id}"

    async def execute(self, action: dict, ctx: ActionContext) -> dict:
        return ActionResult.ok(ref=f"{{self.action_type}}:dry-run",
                               detail="scaffold noop")


def create_adapter() -> ActionAdapter:
    return {cls}Adapter()

def execute(action: dict, ctx: ActionContext) -> dict:
    """约定函数（docs/02）；异步适配器请走 create_adapter。"""
    return {{"ok": True, "ref": "custom.{plugin_id}:sync", "detail": "scaffold"}}
'''


def _template_entry(plugin_id: str, name: str) -> str:
    spec = {
        "id": plugin_id,
        "name": name,
        "description": f"{name}（脚手架）",
        "monitors": [],
        "dsl_rules": [],
        "skills": [],
    }
    return json.dumps(spec, ensure_ascii=False, indent=2) + "\n"


def _readme(ptype: str, plugin_id: str) -> str:
    return f"""# {plugin_id}

类型：`{ptype}`。由 `insflow plugin new` 生成。

```bash
insflow plugin check plugins/{TYPE_TO_DIR[ptype]}/{plugin_id}
# 若生成在其它目录：
insflow plugin install <本目录>
```

规范见仓库 `docs/15-插件开发指南.md`。secret 配置不得带 default，entry 不得含明文密钥。
"""


def create_plugin(ptype: str, plugin_id: str, *,
                  dest: Path | None = None, name: str = "",
                  force: bool = False) -> dict:
    """生成插件目录。返回 {ok, path, check}。"""
    if ptype not in TYPE_TO_DIR:
        return {"ok": False, "error": f"type 非法: {ptype}（合法: {list(TYPE_TO_DIR)}）"}
    err = validate_id(plugin_id)
    if err:
        return {"ok": False, "error": err}

    target = dest or default_dest(ptype, plugin_id)
    if target.exists():
        if not force:
            return {"ok": False, "error": f"目录已存在: {target}（加 --force 覆盖）"}
        shutil.rmtree(target)
    target.mkdir(parents=True)

    display = name or plugin_id
    entry = "template.json" if ptype == "template" else "entry.py"
    (target / "manifest.json").write_text(
        json.dumps(_manifest(ptype, plugin_id, display, entry),
                   ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    if ptype == "source":
        (target / "entry.py").write_text(_source_entry(plugin_id), encoding="utf-8")
    elif ptype == "model":
        (target / "entry.py").write_text(_model_entry(plugin_id), encoding="utf-8")
    elif ptype == "action":
        (target / "entry.py").write_text(_action_entry(plugin_id), encoding="utf-8")
    else:
        (target / "template.json").write_text(
            _template_entry(plugin_id, display), encoding="utf-8")
    (target / "README.md").write_text(_readme(ptype, plugin_id), encoding="utf-8")

    report = check_plugin(target)
    return {
        "ok": True,
        "path": str(target),
        "plugin_id": plugin_id,
        "type": ptype,
        "check": report,
    }
