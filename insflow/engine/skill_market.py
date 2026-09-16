"""Insight Flow Skill 市场格式（v1 定稿）

Skill Pack 格式（跨 Claude / OpenFlow / MFlow 兼容）：
skill-pack/
├── skill-pack.json     # 包清单（id/name/version/skills[]）
└── *.md                # 每个 Skill：YAML frontmatter + Markdown 步骤

格式规范要点：
1. frontmatter 必填：name / description / triggers
2. tools 引用统一命名空间：db.* / source.call:<plugin> / web.fetch / mcp.*
3. frontmatter 值必须是基础类型（兼容 Claude skill 解析器）
4. 单文件单 Skill，文件名 = name
"""

import json
import shutil
from pathlib import Path

from ..agent.skills_host import SkillsHost, parse_skill
from ..core.files import EventBus

ROOT_DIR = Path(__file__).parent.parent.parent
SKILLS_DIR = ROOT_DIR / "skills"
SKILL_MARKET_DIR = ROOT_DIR / "skill-market"

# 统一工具命名空间（跨工具兼容的核心约束）
VALID_TOOL_PREFIXES = ("db.", "source.call:", "web.", "mcp:", "report.")
REQUIRED_FRONTMATTER = ("name", "description", "triggers")


def validate_skill_md(md_text: str) -> tuple[list[str], list[str]]:
    """校验单个 Skill 文件（返回 errors, warnings）"""
    errors, warnings = [], []
    try:
        skill = parse_skill(md_text)
    except ValueError as e:
        return [str(e)], warnings

    for field in REQUIRED_FRONTMATTER:
        if not getattr(skill, field, None):
            errors.append(f"frontmatter 缺少必填字段: {field}")

    for tool in skill.tools:
        if not str(tool).startswith(VALID_TOOL_PREFIXES):
            errors.append(f"工具 {tool} 不在统一命名空间（{VALID_TOOL_PREFIXES}）")

    if skill.inputs and not isinstance(skill.inputs, dict):
        errors.append("inputs 必须是对象")

    if len(skill.body) < 50:
        warnings.append("执行步骤过短（<50 字符），跨工具复用时建议写明步骤")

    return errors, warnings


class SkillMarket:
    """Skill 市场（本地目录 + 手动安装）"""

    def __init__(self, workspace_id: str = "default",
                 skills_dir: Path | None = None, market_dir: Path | None = None):
        self.workspace_id = workspace_id
        self.skills_dir = skills_dir or SKILLS_DIR
        self.market_dir = market_dir or (ROOT_DIR / "skill-market")
        self.bus = EventBus(workspace_id)

    def scan(self) -> list[dict]:
        """列出市场中可安装的 Skill 包"""
        if not self.market_dir.exists():
            return []
        out = []
        for pack_dir in self.market_dir.iterdir():
            if not pack_dir.is_dir():
                continue
            meta_file = pack_dir / "skill-pack.json"
            if not meta_file.exists():
                continue
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            skills = []
            for md_file in pack_dir.glob("*.md"):
                errs, warns = validate_skill_md(md_file.read_text(encoding="utf-8"))
                skills.append({
                    "file": md_file.name,
                    "valid": not errs,
                    "errors": errs,
                    "warnings": warns,
                })
            out.append({
                "pack_id": meta.get("id", pack_dir.name),
                "name": meta.get("name", pack_dir.name),
                "version": meta.get("version", ""),
                "author": meta.get("author", ""),
                "skills": skills,
                "installable": all(s["valid"] for s in skills),
            })
        return out

    def install(self, pack_path: str | Path) -> dict:
        """安装 Skill 包 → skills/ 目录（热加载）"""
        src = Path(pack_path)
        meta_file = src / "skill-pack.json"
        if not src.exists() or not meta_file.exists():
            return {"ok": False, "detail": "skill-pack.json 不存在"}

        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            return {"ok": False, "detail": f"skill-pack.json 解析失败: {e}"}

        pack_id = meta.get("id", src.name)
        installed, failed = [], []
        for md_file in src.glob("*.md"):
            errs, _ = validate_skill_md(md_file.read_text(encoding="utf-8"))
            if errs:
                failed.append({"file": md_file.name, "errors": errs})
                continue
            dest = self.skills_dir / md_file.name
            shutil.copy2(md_file, dest)
            installed.append(md_file.name)

        if installed:
            self.bus.emit("skill.pack_installed", {
                "pack_id": pack_id, "skills": installed,
            })
        return {
            "ok": bool(installed),
            "pack_id": pack_id,
            "installed": installed,
            "failed": failed,
        }

    def export(self, skill_name: str, out_dir: Path) -> dict:
        """导出单个 Skill 为可分发的包（跨工具兼容格式）"""
        host = SkillsHost(self.skills_dir)
        skill = host.get(skill_name)
        if not skill:
            return {"ok": False, "detail": f"Skill 不存在: {skill_name}"}

        src_file = self.skills_dir / f"{skill_name}.md"
        if not src_file.exists():
            return {"ok": False, "detail": "Skill 源文件缺失"}

        pack_dir = out_dir / skill_name
        pack_dir.mkdir(parents=True, exist_ok=True)
        (pack_dir / "skill-pack.json").write_text(json.dumps({
            "id": skill_name,
            "name": skill.name,
            "version": "1.0.0",
            "author": "insight-flow",
            "compat": ["claude", "openflow", "mflow", "insflow"],
            "skills": [f"{skill_name}.md"],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        shutil.copy2(src_file, pack_dir / src_file.name)
        return {"ok": True, "pack_dir": str(pack_dir)}
