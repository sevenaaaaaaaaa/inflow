"""Insight Flow 凭据保险库

AES-GCM 加密落盘，主密钥 INSFLOW_MASTER_KEY（环境变量，未设则 fail-closed）
"""

import base64
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

try:
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC as _PBKDF2
except ImportError:  # 兼容不同 cryptography 版本命名
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2 as _PBKDF2

# 默认保险库路径
DEFAULT_VAULT_PATH = Path(__file__).parent.parent.parent.parent / "data" / "vault.json"


def _vault_path() -> Path:
    """运行时推导（测试/私有化可重定向 data 目录）"""
    from .. import files as files_mod
    return (files_mod.DATA_DIR or DEFAULT_VAULT_PATH.parent) / "vault.json"

# 密钥派生参数
SALT_SIZE = 16
NONCE_SIZE = 12
KDF_ITERATIONS = 480000  # OWASP 推荐


class VaultError(Exception):
    """保险库错误"""
    pass


class Vault:
    """凭据保险库

    使用 AES-256-GCM 加密存储凭据。
    主密钥从环境变量 INSFLOW_MASTER_KEY 派生。
    """

    def __init__(self, vault_path: Path | None = None):
        self.vault_path = vault_path or _vault_path()
        self._master_key: bytes | None = None
        self._data: dict = {}

    def _get_master_key(self) -> bytes:
        """获取主密钥（从环境变量派生）"""
        if self._master_key:
            return self._master_key

        master_password = os.environ.get("INSFLOW_MASTER_KEY")
        if not master_password:
            raise VaultError(
                "INSFLOW_MASTER_KEY 环境变量未设置。"
                "凭据保险库需要主密钥才能工作（fail-closed）。"
            )

        # 使用 PBKDF2 从密码派生密钥
        # 注意：生产环境应使用固定的 salt（存储在 vault 文件中）
        salt = os.environ.get("INSFLOW_VAULT_SALT", "insflow-default-salt").encode()
        kdf = _PBKDF2(
            algorithm=hashes.SHA256(),
            length=32,  # AES-256
            salt=salt,
            iterations=KDF_ITERATIONS,
        )
        self._master_key = kdf.derive(master_password.encode())
        return self._master_key

    def _encrypt(self, plaintext: str) -> str:
        """加密字符串"""
        key = self._get_master_key()
        nonce = os.urandom(NONCE_SIZE)
        aesgcm = AESGCM(key)
        ciphertext = aesgcm.encrypt(nonce, plaintext.encode(), None)
        # 返回 base64(nonce + ciphertext)
        return base64.b64encode(nonce + ciphertext).decode()

    def _decrypt(self, encrypted: str) -> str:
        """解密字符串"""
        key = self._get_master_key()
        data = base64.b64decode(encrypted)
        nonce = data[:NONCE_SIZE]
        ciphertext = data[NONCE_SIZE:]
        aesgcm = AESGCM(key)
        plaintext = aesgcm.decrypt(nonce, ciphertext, None)
        return plaintext.decode()

    def load(self) -> dict:
        """加载保险库"""
        if not self.vault_path.exists():
            self._data = {}
            return self._data

        try:
            raw = json.loads(self.vault_path.read_text())
            self._data = {}
            # 解密所有凭据
            for key, encrypted_value in raw.get("credentials", {}).items():
                try:
                    self._data[key] = self._decrypt(encrypted_value)
                except Exception as e:
                    print(f"Warning: Failed to decrypt credential '{key}': {e}")
            return self._data
        except json.JSONDecodeError:
            raise VaultError("保险库文件格式错误")

    def save(self) -> None:
        """保存保险库"""
        self.vault_path.parent.mkdir(parents=True, exist_ok=True)

        # 加密所有凭据
        encrypted_data = {}
        for key, value in self._data.items():
            encrypted_data[key] = self._encrypt(str(value))

        vault_content = {
            "version": 1,
            "updated_at": datetime.now(UTC).isoformat(),
            "credentials": encrypted_data,
        }

        # 原子写入
        tmp_path = self.vault_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(vault_content, indent=2))
        tmp_path.rename(self.vault_path)

    def get(self, key: str) -> str | None:
        """获取凭据"""
        return self._data.get(key)

    def set(self, key: str, value: str) -> None:
        """设置凭据"""
        self._data[key] = value

    def delete(self, key: str) -> bool:
        """删除凭据"""
        if key in self._data:
            del self._data[key]
            return True
        return False

    def list_keys(self) -> list[str]:
        """列出所有凭据键（不返回值）"""
        return list(self._data.keys())

    def mask_value(self, value: str) -> str:
        """遮蔽敏感值（只显示后四位）"""
        if len(value) <= 4:
            return "****"
        return "****" + value[-4:]


# 全局实例
_vault: Vault | None = None


def get_vault() -> Vault:
    """获取全局保险库实例（DATA_DIR 变化时重建路径）"""
    global _vault
    target = _vault_path()
    if _vault is None or _vault.vault_path != target:
        _vault = Vault(vault_path=target)
        try:
            _vault.load()
        except VaultError:
            # 首次运行可能没有保险库文件
            pass
    return _vault
