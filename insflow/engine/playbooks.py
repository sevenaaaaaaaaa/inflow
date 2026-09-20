"""配方挖掘（Playbook Mining v1）：把"验证有效"的经验沉淀为可复用配方

数据来源：`verification_results`（结构化验证结论）。
产出：`data/playbooks/<workspace>/*.json` 草案（template-pack 兼容形态），
人工确认后可用 `insflow template validate/apply` 下发到新工作区。

原则：只从**有统计依据**的经验里提炼（样本 ≥ MIN_SAMPLES，有效率 ≥ 50%），
并把证据写进草案（样本数/有效率/平均效应/代表动作），不编故事。
"""

import json
import re
from datetime import UTC, datetime
from pathlib import Path

MIN_SAMPLES = 3
MIN_EFFECTIVE_RATE = 0.5


def playbook_dir(workspace_id: str) -> Path:
    from ..core.files import DATA_DIR
    d = Path(DATA_DIR or ".") / "playbooks" / re.sub(r"[^\w.-]", "_", workspace_id)[:48]
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slug(text: str) -> str:
    s = re.sub(r"[^\w-]+", "-", str(text)).strip("-").lower()
    return s[:40] or "playbook"


async def mine(workspace_id: str, *, min_samples: int = MIN_SAMPLES) -> list[dict]:
    """从验证结论里挖配方（按 insight_type × action_type 聚类）"""
    from ..core.store import get_store
    store = await get_store()
    results = await store.list_verification_results(workspace_id, limit=2000)
    clusters: dict[tuple[str, str], dict] = {}
    for r in results:
        key = (str(r.get("insight_type") or "unknown"),
               str(r.get("action_type") or "unknown"))
        entry = clusters.setdefault(key, {
            "samples": 0, "effective": 0, "significant": 0,
            "effects": [], "actions": [], "metrics": set()})
        entry["samples"] += 1
        entry["effective"] += 1 if r.get("verdict") == "effective" else 0
        entry["significant"] += 1 if r.get("significant") else 0
        entry["effects"].append(float(r.get("effect_pct") or 0))
        if r.get("action_id"):
            entry["actions"].append(str(r["action_id"]))
        if r.get("metric"):
            entry["metrics"].add(str(r["metric"]))

    drafts = []
    for (insight_type, action_type), e in clusters.items():
        if e["samples"] < min_samples:
            continue
        rate = e["effective"] / e["samples"]
        if rate < MIN_EFFECTIVE_RATE:
            continue
        avg_effect = sum(e["effects"]) / len(e["effects"]) if e["effects"] else 0
        pid = f"pb-{_slug(insight_type)}-{_slug(action_type)}"
        draft = {
            "id": pid,
            "name": f"{insight_type} → {action_type}（验证有效配方）",
            "industry": "",
            "monitors": [],
            "dsl_rules": [],
            "playbook": {
                "insight_type": insight_type,
                "action_type": action_type,
                "evidence": {
                    "samples": e["samples"],
                    "effective_rate": round(rate, 3),
                    "significant": e["significant"],
                    "avg_effect_pct": round(avg_effect, 4),
                    "metrics": sorted(e["metrics"]),
                    "example_action_ids": e["actions"][:5],
                },
                "when": f"出现 {insight_type} 类洞察且数据质量 fresh 时",
                "then": f"派发 {action_type}，并按 14 天窗口验证",
                "note": ("由 insflow playbook mine 从验证结论自动提炼；"
                         "请人工确认后作为行业模板包使用"),
            },
            "source_note": f"workspace={workspace_id} mined_at={datetime.now(UTC).isoformat()}",
        }
        path = playbook_dir(workspace_id) / f"{pid}.json"
        path.write_text(json.dumps(draft, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        # 结构提案要按 insight_type/action_type 建规则，之前只返回 evidence
        # （类型只能从 id 里反解，多词类型必然解错）→ 一并带出来
        drafts.append({"id": pid, "path": str(path),
                       "insight_type": insight_type, "action_type": action_type,
                       **draft["playbook"]["evidence"]})
    drafts.sort(key=lambda d: (-d["effective_rate"], -d["samples"]))
    return drafts


def list_playbooks(workspace_id: str) -> list[dict]:
    out = []
    for f in sorted(playbook_dir(workspace_id).glob("*.json")):
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        pb = doc.get("playbook") or {}
        out.append({"id": doc.get("id") or f.stem, "name": doc.get("name", ""),
                    "evidence": pb.get("evidence") or {}, "path": str(f)})
    return out


def export_template(workspace_id: str, playbook_id: str, out_path: str) -> dict:
    """把配方导出为 template-pack 兼容文件（用官方校验器验证）"""
    src = playbook_dir(workspace_id) / f"{playbook_id}.json"
    if not src.exists():
        return {"ok": False, "detail": f"配方不存在：{playbook_id}"}
    doc = json.loads(src.read_text(encoding="utf-8"))
    from .template_pack import validate_template
    spec = {"id": doc.get("id"), "name": doc.get("name"),
            "monitors": doc.get("monitors") or [],
            "dsl_rules": doc.get("dsl_rules") or []}
    errors = validate_template(spec)
    if errors:
        return {"ok": False, "detail": f"模板校验失败：{errors}"}
    Path(out_path).write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    return {"ok": True, "path": out_path, "id": doc.get("id")}
