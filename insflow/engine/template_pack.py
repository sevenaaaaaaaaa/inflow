"""Insight Flow 行业模板包（R3-1）

把首个客户的交付经验产品化：新客户选行业即带完整配置。

模板包结构（JSON，templates/industry/<name>.json）：
{
  "id": "saas-growth",
  "name": "SaaS 增长模板",
  "description": "SaaS 工具增长情报标配",
  "monitors": [                      // 监控项清单
    {"kind": "site_change", "target": {"url": "..." }, "schedule_cron": "0 */6 * * *"},
    {"kind": "keyword", "target": {"site": "sc-domain:..."}, "schedule_cron": "0 2 * * *"}
  ],
  "dsl_rules": [ {..DSL spec..} ],   // 自定义规则模型
  "skills": ["competitor-move-analysis", "weekly-brief-writer"]
}

apply(workspace)：创建监控 + 注册 DSL 规则 + 事件流审计，一键复制交付配置。
"""

import json
from pathlib import Path

from ..core.files import EventBus
from ..core.store import get_store
from .dsl_models import get_dsl_registry, save_rule, validate_dsl
from .monitors import MonitorService

VALID_KINDS = ("site_change", "keyword", "brand_mention", "topic", "journey")


def validate_template(spec: dict) -> list[str]:
    """模板包静态校验（与新客户配置安全边界）"""
    errors = []
    if not spec.get("id"):
        errors.append("缺少 id")
    for m in spec.get("monitors", []):
        if m.get("kind") not in VALID_KINDS:
            errors.append(f"非法监控类型: {m.get('kind')}")
    for rule in spec.get("dsl_rules", []):
        errs = validate_dsl(rule)
        errors.extend(f"DSL[{rule.get('id', '?')}]: {e}" for e in errs)
    return errors


class TemplatePack:
    """单个行业模板包"""

    def __init__(self, spec: dict):
        errors = validate_template(spec)
        if errors:
            raise ValueError(f"模板包校验失败: {errors}")
        self.spec = spec

    @property
    def id(self) -> str:
        return self.spec["id"]

    @property
    def name(self) -> str:
        return self.spec.get("name", self.id)

    async def apply(self, workspace_id: str) -> dict:
        """一键应用：建监控 + 注册 DSL 规则（幂等：重复 apply 跳过已存在的）"""
        store = await get_store()
        bus = EventBus(workspace_id)
        svc = _monitor_service(workspace_id)

        created_monitors = []
        skipped_monitors = []
        for m in self.spec.get("monitors", []):
            existing = await store._fetchall(
                "SELECT id FROM monitors WHERE workspace_id = ? AND kind = ?",
                (workspace_id, m["kind"]))
            if any(True for _ in existing):
                skipped_monitors.append(m["kind"])
                continue
            mon = await svc.create(m["kind"], m.get("target", {}),
                                   m.get("schedule_cron", "0 */6 * * *"))
            created_monitors.append(mon["id"])

        registered_rules = []
        for rule in self.spec.get("dsl_rules", []):
            # 落库（此前只 register 进内存 → 重启后模板带来的规则全丢）
            await save_rule(workspace_id, rule)
            registered_rules.append(rule["id"])

        bus.emit("template.applied", {
            "template": self.id,
            "monitors_created": len(created_monitors),
            "monitors_skipped": len(skipped_monitors),
            "dsl_rules": registered_rules,
        })
        return {
            "ok": True,
            "template": self.id,
            "monitors_created": created_monitors,
            "monitors_skipped": skipped_monitors,
            "dsl_rules_registered": registered_rules,
        }


class TemplateRegistry:
    """模板包目录（templates/packs/*.json）"""

    def __init__(self, root: "Path | None" = None):
        from pathlib import Path
        self.root = root or Path(__file__).parent.parent.parent / "templates" / "packs"
        self._packs: dict[str, TemplatePack] | None = None

    def load(self) -> dict[str, TemplatePack]:
        if self._packs is not None:
            return self._packs
        self._packs = {}
        if self.root.exists():
            for f in sorted(self.root.glob("*.json")):
                try:
                    spec = json.loads(f.read_text(encoding="utf-8"))
                    pack = TemplatePack(spec)
                    self._packs[pack.id] = pack
                except (ValueError, json.JSONDecodeError) as e:
                    print(f"模板加载失败 {f.name}: {e}")
        return self._packs

    def list(self) -> list[dict]:
        return [{"id": p.id, "name": p.name,
                 "monitors": len(p.spec.get("monitors", [])),
                 "dsl_rules": len(p.spec.get("dsl_rules", []))}
                for p in self.load().values()]

    def get(self, pack_id: str) -> TemplatePack | None:
        return self.load().get(pack_id)


def _monitor_service(workspace_id: str):
    from ..core.scheduler import get_scheduler
    return MonitorService(workspace_id, scheduler=get_scheduler())


# ========== 经验沉淀：工作区配置 → 行业模板包（R3-1） ==========

async def export_from_workspace(workspace_id: str, *, template_id: str = "",
                                name: str = "", industry: str = "",
                                include_dsl: bool = True) -> dict:
    """把某个工作区的监控/DSL 规则沉淀成可复用模板包（脱敏：不含数据与账号）"""
    from ..core.store import get_store
    store = await get_store()
    monitors = await store.list_monitors_full(workspace_id)
    spec = {
        "id": template_id or f"custom-{workspace_id[:8]}",
        "name": name or f"{workspace_id} 配置沉淀",
        "industry": industry,
        "monitors": [],
        "dsl_rules": [],
        "source_note": ("由 insflow template export 从工作区导出；"
                        "已剔除数据、账号与密钥，仅保留配置形态"),
    }
    for m in monitors:
        spec["monitors"].append({
            "kind": m.get("kind"),
            "target": dict(m.get("target_json") or {}),
            "schedule_cron": m.get("schedule_cron") or "0 */6 * * *",
        })
    if include_dsl:
        # 曾写成 registry.list(workspace_id)（方法名是 list_for）→ AttributeError
        # 被 except 吞掉，导出的模板包里 dsl_rules 永远是空的，且没有任何提示
        from .dsl_models import persisted_rules
        registry = get_dsl_registry()
        seen = {r.get("id") for r in registry.list_for(workspace_id)}
        spec["dsl_rules"].extend(registry.list_for(workspace_id))
        for rule in await persisted_rules(workspace_id):
            if rule.get("id") not in seen:
                spec["dsl_rules"].append(rule)
    errors = validate_template(spec)
    return {"spec": spec, "errors": errors,
            "monitors": len(spec["monitors"]), "dsl_rules": len(spec["dsl_rules"])}
