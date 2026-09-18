"""测试行业模板包（校验/加载/一键应用）"""


import pytest

import insflow.core.files as files_mod
from insflow.engine.template_pack import (
    TemplatePack,
    TemplateRegistry,
    validate_template,
)

SAAS_SPEC = {
    "id": "saas-growth",
    "name": "SaaS 增长模板",
    "monitors": [
        {"kind": "site_change", "target": {"url": "https://c.com/pricing"}},
        {"kind": "keyword", "target": {"site": "sc-domain:x.com"}},
        {"kind": "brand_mention", "target": {"query": "automation"}},
    ],
    "dsl_rules": [{
        "id": "cac-overrun", "metric": "cac",
        "condition": {"op": ">=", "value": 500},
        "severity": "high",
    }],
    "skills": ["weekly-brief-writer"],
}

ECOM_SPEC = {
    "id": "ecommerce-retention",
    "name": "电商留存模板",
    "monitors": [{"kind": "site_change", "target": {"url": "https://shop.com"}}],
    "dsl_rules": [],
    "skills": [],
}


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)
    monkeypatch.setenv("INSFLOW_MASTER_KEY", "mk")
    from insflow.core.entities import Workspace
    from insflow.core.store import Store, reset_store

    s = Store(db_path=tmp_path / "test.db")
    await s.connect()
    await s.migrate()
    reset_store(s)
    await s.create_workspace(Workspace(id="test-ws", name="Test WS"))
    yield {"store": s}
    reset_store(None)
    await s.close()


class TestValidation:
    async def test_valid(self):
        assert validate_template(SAAS_SPEC) == []

    async def test_bad_kind(self):
        errors = validate_template({**SAAS_SPEC, "monitors": [{"kind": "bad"}]})
        assert any("非法监控类型" in e for e in errors)

    async def test_bad_dsl(self):
        bad = {**SAAS_SPEC, "dsl_rules": [{"id": "r", "metric": ""}]}
        errors = validate_template(bad)
        assert any("DSL" in e for e in errors)


class TestApply:
    async def test_apply_creates_monitors_and_rules(self, env):
        pack = TemplatePack(SAAS_SPEC)
        result = await pack.apply("test-ws")

        assert result["ok"] is True
        assert len(result["monitors_created"]) == 3

        from insflow.engine.dsl_models import get_dsl_registry
        assert get_dsl_registry().get("test-ws", "cac-overrun")

    async def test_apply_idempotent(self, env):
        """重复应用：跳过已存在的监控（不重复建）"""
        pack = TemplatePack(SAAS_SPEC)
        first = await pack.apply("test-ws")
        second = await pack.apply("test-ws")

        assert len(first["monitors_created"]) == 3
        assert not second["monitors_created"]
        assert len(second["monitors_skipped"]) == 3

        from insflow.core.store import get_store
        monitors = await (await get_store()).list_monitors_full("test-ws")
        assert len(monitors) == 3


class TestRegistry:
    async def test_builtin_packs_loaded(self):
        items = TemplateRegistry().list()
        ids = {p["id"] for p in items}
        assert {"saas-growth", "ecommerce-retention"} <= ids

    async def test_get_pack(self, env):
        pack = TemplateRegistry().get("saas-growth")
        assert pack.name == "SaaS 增长模板"
