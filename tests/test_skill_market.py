"""测试 Skill 市场格式（pack 校验/安装/导出）"""

import json

import pytest

import insflow.engine.skill_market as sm
from insflow.engine.skill_market import SkillMarket, validate_skill_md

GOOD_SKILL = """---
name: demo-analysis
description: 演示分析 Skill
triggers: [demo, 演示]
tools: [db.query, source.call:serper]
inputs: {topic: string}
---

# 执行步骤

1. 用 db.query 拉取洞察
2. 用 source.call:serper 交叉验证
3. 输出带证据引用的结论
"""


@pytest.fixture
def market(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    mk_dir = tmp_path / "skill-market" / "demo-pack"
    mk_dir.mkdir(parents=True)

    (mk_dir / "skill-pack.json").write_text(json.dumps({
        "id": "demo-pack", "name": "演示包", "version": "1.0.0",
        "author": "tester",
    }))
    (mk_dir / "demo-analysis.md").write_text(GOOD_SKILL)

    monkeypatch.setattr(sm, "ROOT_DIR", tmp_path)
    import insflow.core.files as files_mod
    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path / "data")

    return SkillMarket("test-ws", skills_dir=skills_dir, market_dir=tmp_path / "skill-market")


class TestValidation:
    def test_valid(self):
        errs, warns = validate_skill_md(GOOD_SKILL)
        assert errs == []

    def test_missing_frontmatter(self):
        errs, _ = validate_skill_md("# 没有 frontmatter")
        assert errs

    def test_bad_tool_namespace(self):
        md = GOOD_SKILL.replace("source.call:serper", "http://some-tool.com")
        errs, _ = validate_skill_md(md)
        assert any("命名空间" in e for e in errs)


class TestSkillMarket:
    def test_scan(self, market):
        packs = market.scan()
        assert len(packs) == 1
        assert packs[0]["pack_id"] == "demo-pack"
        assert packs[0]["installable"] is True

    def test_install(self, market):
        pack_dir = market.market_dir / "demo-pack"
        result = market.install(pack_dir)
        assert result["ok"] is True
        assert result["installed"] == ["demo-analysis.md"]
        assert (market.skills_dir / "demo-analysis.md").exists()

    def test_install_skips_invalid(self, market):
        pack_dir = market.market_dir / "demo-pack"
        (pack_dir / "bad.md").write_text("invalid")
        result = market.install(pack_dir)
        assert result["ok"] is True
        assert result["failed"][0]["file"] == "bad.md"
        assert not (market.skills_dir / "bad.md").exists()

    def test_export(self, market, tmp_path):
        (market.skills_dir / "demo-analysis.md").write_text(GOOD_SKILL)
        out = tmp_path / "export"
        result = market.export("demo-analysis", out)
        assert result["ok"] is True
        pack = out / "demo-analysis"
        assert (pack / "skill-pack.json").exists()
        meta = json.loads((pack / "skill-pack.json").read_text())
        assert "claude" in meta["compat"]

    def test_export_missing(self, market, tmp_path):
        result = market.export("ghost", tmp_path)
        assert result["ok"] is False
