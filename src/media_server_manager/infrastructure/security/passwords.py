from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path

from media_server_manager.config import settings
from media_server_manager.infrastructure.db.repositories.auth import AuthRepository
from media_server_manager.infrastructure.db.runtime import init_db
from media_server_manager.infrastructure.db.session import Database


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


def load_session_secret(db_file: Path | None = None) -> str:
    configured = os.environ.get("MSM_SECRET_KEY", "").strip()
    if configured:
        return configured
    db_path = Path(db_file or settings.database_path)
    init_db(db_path)
    legacy_secret = db_path.parent / "session_secret"
    with Database.for_path(db_path).transaction() as uow:
        auth = AuthRepository(uow.session)
        value = auth.setting("session_secret")
        if not value and legacy_secret.exists():
            value = legacy_secret.read_text(encoding="ascii").strip()
        if len(value) < 32:
            value = secrets.token_urlsafe(48)
        auth.set_setting("session_secret", value)
    if legacy_secret.exists():
        try:
            legacy_secret.unlink()
        except OSError:
            pass
    if len(value) < 32:
        raise RuntimeError("会话密钥文件无效，请删除后重新启动。")
    return value


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def verify_csrf(expected: str, supplied: str) -> bool:
    return bool(expected and supplied and hmac.compare_digest(expected, supplied))

