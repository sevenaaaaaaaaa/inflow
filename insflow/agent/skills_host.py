"""Insight Flow Skill 宿主

Skill = Agent 的方法论（Markdown + YAML frontmatter，跨工具兼容格式）。
插件 = 确定性能力；Skill = Agent 怎么分析、怎么写报告。
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

# 解析 YAML 的轻量实现（避免强依赖 PyYAML……虽然本仓库已有 pyyaml）
import yaml

SKILLS_DIR = Path(__file__).parent.parent.parent / "skills"


@dataclass
class Skill:
    """Agent Skill"""
    name: str
    description: str
    triggers: list[str] = field(default_factory=list)
    inputs: dict = field(default_factory=dict)
    tools: list[str] = field(default_factory=list)
    output_template: str = ""
    body: str = ""  # Markdown 执行步骤

    def to_system_prompt(self) -> str:
        """渲染为 system prompt 片段"""
        front = (
            f"Skill: {self.name}\n"
            f"描述: {self.description}\n"
            f"工具: {', '.join(self.tools)}\n"
        )
        return front + "\n" + self.body


def parse_skill(md_text: str) -> Skill:
    """解析 Markdown + YAML frontmatter 格式的 Skill"""
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", md_text, re.DOTALL)
    if not match:
        raise ValueError("Skill 文件缺少 YAML frontmatter（--- 包裹的头部）")

    meta = yaml_safe_load(match.group(1))
    body = match.group(2).strip()

    return Skill(
        name=meta.get("name", ""),
        description=meta.get("description", ""),
        triggers=meta.get("triggers", []) or [],
        inputs=meta.get("inputs", {}) or {},
        tools=meta.get("tools", []) or [],
        output_template=meta.get("output_template", ""),
        body=body,
    )


def yaml_safe_load(text: str) -> dict:
    """加载 YAML frontmatter（PyYAML 已在依赖中）"""
    import yaml
    data = yaml.safe_load(text)
    return data if isinstance(data, dict) else {}


class SkillsHost:
    """Skill 加载与匹配"""

    def __init__(self, skills_dir: Path | None = None):
        self.skills_dir = skills_dir or SKILLS_DIR
        self._skills: dict[str, Skill] | None = None

    def load(self) -> dict[str, Skill]:
        """加载 skills/ 目录下全部 .md"""
        if self._skills is not None:
            return self._skills
        self._skills = {}
        if self.skills_dir.exists():
            for md_file in sorted(self.skills_dir.glob("*.md")):
                try:
                    skill = parse_skill(md_file.read_text(encoding="utf-8"))
                    if skill.name:
                        self._skills[skill.name] = skill
                except (ValueError, Exception) as e:
                    print(f"Skill 加载失败 {md_file.name}: {e}")
        return self._skills

    def get(self, name: str) -> Skill | None:
        return self.load().get(name)

    def match(self, question: str) -> list[Skill]:
        """按触发词匹配最相关的 Skill（命中排序，全部命中则空）"""
        skills = self.load()
        matched: list[Skill] = []
        ql = question.lower()
        for skill in skills.values():
            score = sum(1 for t in skill.triggers if str(t).lower() in ql)
            if score > 0:
                matched.append((score, skill))
        matched.sort(key=lambda x: -x[0])
        return [s for _, s in matched]

    def list_skills(self) -> list[dict]:
        return [{
            "name": s.name,
            "description": s.description,
            "triggers": s.triggers,
        } for s in self.load().values()]


# 全局实例
_host: SkillsHost | None = None


def get_skills_host() -> SkillsHost:
    global _host
    if _host is None:
        _host = SkillsHost()
    return _host
