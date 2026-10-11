from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ...infrastructure.db.repositories.auth import AuthRepository


class AuthService:
    def __init__(self, session: Session) -> None:
        self.repository = AuthRepository(session)

    def has_users(self) -> bool:
        return self.repository.has_users()

    def current_user_exists(self, user_id: int | None) -> bool:
        return bool(user_id and self.repository.get_user(user_id))

    def create_user(self, username: str, password_hash: str):
        return self.repository.create_user(username, password_hash)

    def authenticate(self, username: str, ip_address: str, password: str, verify_password) -> tuple[object | None, bool]:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
        self.repository.prune_attempts(cutoff)
        if self.repository.recent_attempt_count(username, ip_address, cutoff) >= 5:
            return None, True
        user = self.repository.by_username(username)
        if user is None or not verify_password(password, user.password_hash):
            self.repository.record_attempt(username, ip_address)
            return None, False
        self.repository.clear_attempts(username, ip_address)
        return user, False

    def change_password(self, user_id: int, current_password: str, new_password: str, verify_password, password_hash):
        user = self.repository.get_user(user_id)
        if user is None or not verify_password(current_password, user.password_hash):
            return False
        user.password_hash = password_hash(new_password)
        user.updated_at = datetime.now(timezone.utc)
        return True

    def preferences(self, user_id: int):
        return self.repository.preferences(user_id)

    def set_preference(self, user_id: int, key: str, value: str) -> None:
        self.repository.set_preference(user_id, key, value)
