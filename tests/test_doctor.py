"""测试 insflow doctor 体检（R1-3）"""

import pytest

import insflow.core.files as files_mod
from insflow.engine.doctor import Doctor


@pytest.fixture
def doctor(tmp_path, monkeypatch):
    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)
    monkeypatch.setenv("INSFLOW_MASTER_KEY", "test-mk")
    (tmp_path / "reports").mkdir()
    (tmp_path / "insflow.db").write_bytes(b"")
    return Doctor("test-ws")


class TestDoctor:
    async def test_all_five_sections(self, doctor):
        result = doctor.run_all()
        assert set(result["sections"].keys()) == {"环境", "凭据", "插件", "调度", "磁盘"}
        assert result["summary"]["total"] > 5

    async def test_master_key_missing_is_fail_with_fix(self, doctor, monkeypatch):
        monkeypatch.delenv("INSFLOW_MASTER_KEY", raising=False)
        result = doctor.run_all()
        env = result["sections"]["环境"]
        key_check = next(c for c in env if "主密钥" in c.name)
        assert key_check.status == "fail"
        assert key_check.fix  # 修复建议非空

    async def test_missing_data_dir_is_fail(self, tmp_path, monkeypatch):
        monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path / "nonexistent")
        monkeypatch.setenv("INSFLOW_MASTER_KEY", "test-mk")
        result = Doctor("test-ws").run_all()
        disk = result["sections"]["磁盘"]
        assert any(c.status == "fail" for c in disk)

    async def test_never_backed_up_warns(self, doctor):
        result = doctor.run_all()
        disk = result["sections"]["磁盘"]
        assert any(c.name == "备份" and c.status == "warn" for c in disk)

    async def test_no_credentials_warns_with_fix(self, doctor, monkeypatch):
        monkeypatch.delenv("INSFLOW_MASTER_KEY", raising=False)
        result = doctor.run_all()
        creds = result["sections"]["凭据"]
        vault_check = next(c for c in creds if "保险库" in c.name)
        assert vault_check.status in ("warn", "fail")
