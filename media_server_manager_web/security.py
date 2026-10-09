from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path

from media_server_manager.core import CONFIG_DIR

SESSION_SECRET_FILE = CONFIG_DIR / "session_secret"


def hash_password(password: str) -> str:
    salt = os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("ascii"), 260000)
    return f"pbkdf2_sha256${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, salt, expected = stored.split("$", 2)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("ascii"), 260000)
    return hmac.compare_digest(digest.hex(), expected)


def load_session_secret(secret_file: Path = SESSION_SECRET_FILE) -> str:
    configured = os.environ.get("MSM_SECRET_KEY", "").strip()
    if configured:
        return configured
    secret_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        with secret_file.open("x", encoding="ascii") as handle:
            handle.write(secrets.token_urlsafe(48))
        os.chmod(secret_file, 0o600)
    except FileExistsError:
        pass
    value = secret_file.read_text(encoding="ascii").strip()
    if len(value) < 32:
        raise RuntimeError("会话密钥文件无效，请删除后重新启动。")
    return value


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def verify_csrf(expected: str, supplied: str) -> bool:
    return bool(expected and supplied and hmac.compare_digest(expected, supplied))
