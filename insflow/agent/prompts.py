"""Prompt 版本化注册表（prompts/<name>.v<N>.md）

为什么要版本号：prompt 改一个字就是一次"模型行为变更"，但既没有 diff 记录、
也没法回测——这是 AI native 维度最典型的黑箱。约定：
- 文件名即版本：`prompts/agent_system.v2.md`（v 必须是正整数，单调递增）
- 文件头可带极简 frontmatter（`---` 包裹的 `key: value`，不引 YAML 依赖）
- 每次取用都返回 `hash`（正文 blake2b 前 8 字节），答案里回传 → 事后可定位是哪版说的
- 生效版本：环境变量 `INSFLOW_PROMPT_<NAME>` > 工作区 `settings_json.prompt_versions`
  > 目录里最大的版本号

打包安装（无 prompts/ 目录）时回落到代码内置的 `fallback`，服务永不因缺文件而挂。
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

FILE_RE = re.compile(r"^(?P<name>[a-z][a-z0-9_]*)\.v(?P<version>\d+)\.md$")


def prompts_dir() -> Path:
    """prompts 目录（INSFLOW_PROMPTS_DIR 可覆盖：镜像/多实例挂载自定义 prompt）"""
    override = os.environ.get("INSFLOW_PROMPTS_DIR", "").strip()
    if override:
        return Path(override)
    from ..core.files import ROOT_DIR
    return Path(ROOT_DIR) / "prompts"


@dataclass
class Prompt:
    name: str
    version: int
    text: str
    hash: str
    meta: dict
    path: str = ""

    @property
    def ref(self) -> str:
        """答案里回传的引用串：agent_system@v2#1a2b3c4d"""
        return f"{self.name}@v{self.version}#{self.hash}"


def _parse(raw: str) -> tuple[dict, str]:
    """极简 frontmatter：`---\\nkey: value\\n---\\n正文`"""
    meta: dict = {}
    body = raw
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) == 3:
            for line in parts[1].splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    meta[k.strip()] = v.strip()
            body = parts[2]
    return meta, body.strip()


def text_hash(text: str) -> str:
    return hashlib.blake2b(text.encode("utf-8"), digest_size=4).hexdigest()


def list_versions(name: str) -> list[int]:
    base = prompts_dir()
    if not base.exists():
        return []
    out = []
    for path in base.iterdir():
        m = FILE_RE.match(path.name)
        if m and m.group("name") == name:
            out.append(int(m.group("version")))
    return sorted(out)


def list_prompts() -> list[dict]:
    """目录里所有 prompt 的版本清单"""
    base = prompts_dir()
    found: dict[str, list[int]] = {}
    if base.exists():
        for path in sorted(base.iterdir()):
            m = FILE_RE.match(path.name)
            if m:
                found.setdefault(m.group("name"), []).append(int(m.group("version")))
    return [{"name": n, "versions": sorted(v), "latest": max(v)}
            for n, v in sorted(found.items())]


def _env_version(name: str) -> int | None:
    raw = os.environ.get(f"INSFLOW_PROMPT_{name.upper()}", "").strip().lstrip("vV")
    return int(raw) if raw.isdigit() else None


def get_prompt(name: str, version: int | None = None, *,
               fallback: str = "", settings: dict | None = None) -> Prompt:
    """取用 prompt：显式版本 > 环境变量 > 工作区设置 > 目录最大版本 > fallback"""
    want = version or _env_version(name)
    if want is None and settings:
        raw = (settings.get("prompt_versions") or {}).get(name)
        if isinstance(raw, int) or (isinstance(raw, str) and str(raw).isdigit()):
            want = int(raw)
    versions = list_versions(name)
    if versions:
        chosen = want if want in versions else max(versions)
        path = prompts_dir() / f"{name}.v{chosen}.md"
        try:
            meta, body = _parse(path.read_text(encoding="utf-8"))
            return Prompt(name=name, version=chosen, text=body, hash=text_hash(body),
                          meta=meta, path=str(path))
        except OSError:
            pass
    body = (fallback or "").strip()
    return Prompt(name=name, version=0, text=body, hash=text_hash(body),
                  meta={"source": "fallback"}, path="")
