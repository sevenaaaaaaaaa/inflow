"""测试插件市场机制（scan/install/uninstall/审计）"""

import json

import pytest

import insflow.engine.marketplace as mp
from insflow.engine.marketplace import Marketplace, check_plugin

GOOD_PLUGIN = {
    "id": "demo-source",
    "type": "source",
    "name": "Demo Source",
    "version": "1.0.0",
    "entry": "entry.py",
    "permissions": ["network"],
    "config": {"api_key": {"desc": "k", "required": True, "secret": True}},
}


@pytest.fixture
def market(tmp_path, monkeypatch):
    """市场环境（重定向目录）"""
    plugins_dir = tmp_path / "plugins"
    mk_dir = tmp_path / "marketplace" / "sources"
    (mk_dir / "demo-source").mkdir(parents=True)
    (plugins_dir).mkdir()

    manifest = json.dumps(GOOD_PLUGIN)
    (mk_dir / "demo-source" / "manifest.json").write_text(manifest)
    (mk_dir / "demo-source" / "entry.py").write_text("def create_plugin():\n    return None\n")

    monkeypatch.setattr(mp, "PLUGINS_DIR", plugins_dir)
    monkeypatch.setattr(mp, "MARKETPLACE_DIR", tmp_path / "marketplace")

    # 重定向 EventBus DATA_DIR（审计文件不落仓库）
    import insflow.core.files as files_mod
    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path / "data")

    return Marketplace("test-ws")


class TestCheckPlugin:
    def test_good(self, tmp_path):
        d = tmp_path / "demo-source"
        d.mkdir()
        (d / "manifest.json").write_text(json.dumps(GOOD_PLUGIN))
        (d / "entry.py").write_text("def create_plugin():\n    return None\n")
        report = check_plugin(d)
        assert report["passed"]
        assert report["errors"] == []

    def test_leaked_secret_detected(self, tmp_path):
        d = tmp_path / "demo-source"
        d.mkdir()
        (d / "manifest.json").write_text(json.dumps(GOOD_PLUGIN))
        (d / "entry.py").write_text('API_KEY = "sk-abcdefghijklmnopqrstuvwx1234"\n')
        report = check_plugin(d)
        assert not report["passed"]
        assert any("明文密钥" in e for e in report["errors"])

    def test_secret_default_rejected(self, tmp_path):
        bad = {**GOOD_PLUGIN, "config": {"api_key": {"secret": True, "default": "abc"}}}
        d = tmp_path / "demo-source"
        d.mkdir()
        (d / "manifest.json").write_text(json.dumps(bad))
        (d / "entry.py").write_text("def create_plugin():\n    return None\n")
        report = check_plugin(d)
        assert not report["passed"]


class TestMarketplace:
    def test_scan(self, market):
        items = market.scan()
        assert len(items) == 1
        assert items[0]["id"] == "demo-source"
        assert items[0]["check_passed"]

    def test_install(self, market):
        src = market.marketplace_dir / "sources" / "demo-source"
        result = market.install(src)
        assert result["ok"] is True
        assert result["auto_scheduled"] is True
        assert (mp.PLUGINS_DIR / "sources" / "demo-source" / "manifest.json").exists()

    def test_install_duplicate_rejected(self, market):
        src = market.marketplace_dir / "sources" / "demo-source"
        market.install(src)
        again = market.install(src)
        assert again["ok"] is False
        assert again["stage"] == "exists"

    def test_install_failing_check(self, market, tmp_path):
        """未过检插件可装入但不会被调度器执行"""
        d = tmp_path / "bad-source"
        d.mkdir()
        (d / "manifest.json").write_text(json.dumps({**GOOD_PLUGIN, "entry": "missing.py"}))
        result = market.install(d)
        assert result["ok"] is True
        assert result["auto_scheduled"] is False  # 不会被自动执行

    def test_uninstall(self, market):
        src = market.marketplace_dir / "sources" / "demo-source"
        market.install(src)
        result = market.uninstall("demo-source")
        assert result["ok"] is True
        assert not (mp.PLUGINS_DIR / "sources" / "demo-source").exists()

    def test_installed_list(self, market):
        src = market.marketplace_dir / "sources" / "demo-source"
        market.install(src)
        installed = market.installed()
        assert {"id": "demo-source", "type": "source",
                "name": "Demo Source", "version": "1.0.0"} in installed
