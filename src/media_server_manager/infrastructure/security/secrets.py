from __future__ import annotations

import base64
import hashlib
import os

from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from ...config import settings


def _runtime_key() -> str:
    # Environment overrides remain useful for tests and rotation commands;
    # normal application code reads the canonical Settings object.
    return os.environ.get("MSM_LOCAL_ENCRYPTION_KEY", "").strip() or settings.encryption_key.strip()


class SecretBox:
    """Small authenticated encryption adapter for secrets stored in SQLite.

    The key is supplied by the deployment environment.  The implementation
    uses AES-GCM from ``cryptography`` and prefixes values so imports can
    distinguish encrypted values from legacy plaintext rows.
    """

    prefix = "enc:v1:"

    @classmethod
    def is_encrypted(cls, value: str | None) -> bool:
        return bool(value and value.startswith(cls.prefix))

    def __init__(self, key: str | bytes | None = None) -> None:
        raw = key if key is not None else _runtime_key()
        if isinstance(raw, str):
            raw = raw.encode()
        if not raw:
            raise ValueError("MSM_LOCAL_ENCRYPTION_KEY 未配置")
        self.key = hashlib.sha256(raw).digest()

    def encrypt(self, value: str) -> str:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = os.urandom(12)
        payload = AESGCM(self.key).encrypt(nonce, value.encode("utf-8"), None)
        encoded = base64.urlsafe_b64encode(nonce + payload).decode("ascii")
        return f"{self.prefix}{encoded}"

    def decrypt(self, value: str) -> str:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        if not value.startswith(self.prefix):
            return value
        raw = base64.urlsafe_b64decode(value[len(self.prefix) :].encode("ascii"))
        return AESGCM(self.key).decrypt(raw[:12], raw[12:], None).decode("utf-8")

    def reencrypt(self, value: str, new_box: "SecretBox") -> str:
        """Decrypt a legacy/plain value and encrypt it with ``new_box``."""
        return new_box.encrypt(self.decrypt(value))


class EncryptedText(TypeDecorator[str]):
    """SQLAlchemy column type that encrypts new secret values transparently."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, _dialect):
        if value is None or value.startswith(SecretBox.prefix):
            return value
        key = _runtime_key()
        return SecretBox(key).encrypt(value) if key else value

    def process_result_value(self, value: str | None, _dialect):
        if value is None:
            return None
        key = _runtime_key()
        return SecretBox(key).decrypt(value) if key and value.startswith(SecretBox.prefix) else value
