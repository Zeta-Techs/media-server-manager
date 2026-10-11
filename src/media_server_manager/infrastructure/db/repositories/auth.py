from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.orm import Session

from ..models import AuthAttempt, OauthFlows, Setting, User, UserPreference


class AuthRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def has_users(self) -> bool:
        return int(self.session.scalar(select(func.count(User.id))) or 0) > 0

    def get_user(self, user_id: int) -> User | None:
        return self.session.get(User, user_id)

    def by_username(self, username: str) -> User | None:
        return self.session.scalar(select(User).where(User.username == username))

    def create_user(self, username: str, password_hash: str) -> User:
        now = datetime.now(timezone.utc)
        user = User(username=username, password_hash=password_hash, created_at=now, updated_at=now)
        self.session.add(user)
        self.session.flush()
        return user

    def recent_attempt_count(self, username: str, ip_address: str, cutoff: datetime) -> int:
        return int(
            self.session.scalar(
                select(func.count(AuthAttempt.id)).where(
                    AuthAttempt.username == username,
                    AuthAttempt.ip_address == ip_address,
                    AuthAttempt.created_at >= cutoff,
                )
            )
            or 0
        )

    def prune_attempts(self, cutoff: datetime) -> None:
        self.session.execute(delete(AuthAttempt).where(AuthAttempt.created_at < cutoff))

    def record_attempt(self, username: str, ip_address: str) -> None:
        self.session.add(
            AuthAttempt(
                username=username,
                ip_address=ip_address,
                created_at=datetime.now(timezone.utc),
            )
        )

    def clear_attempts(self, username: str, ip_address: str) -> None:
        self.session.execute(
            delete(AuthAttempt).where(AuthAttempt.username == username, AuthAttempt.ip_address == ip_address)
        )

    def preferences(self, user_id: int) -> list[UserPreference]:
        return list(
            self.session.scalars(
                select(UserPreference).where(UserPreference.user_id == user_id).order_by(UserPreference.key)
            )
        )

    def set_preference(self, user_id: int, key: str, value: str) -> None:
        preference = self.session.get(UserPreference, (user_id, key))
        now = datetime.now(timezone.utc)
        if preference is None:
            self.session.add(UserPreference(user_id=user_id, key=key, value=value, updated_at=now))
        else:
            preference.value = value
            preference.updated_at = now

    def purge_oauth_flows(self, now: str) -> None:
        self.session.execute(delete(OauthFlows).where(OauthFlows.__table__.c.expires_at < now))

    def create_oauth_flow(
        self, flow_id: str, user_id: int, pin_id: int, expires_at: str, created_at: str
    ) -> None:
        self.session.execute(
            insert(OauthFlows).values(
                id=flow_id,
                user_id=user_id,
                pin_id=pin_id,
                token="",
                resources="[]",
                expires_at=expires_at,
                created_at=created_at,
            )
        )

    def oauth_flow(self, flow_id: str, user_id: int) -> dict[str, object] | None:
        row = self.session.execute(
            select(OauthFlows.__table__).where(
                OauthFlows.__table__.c.id == flow_id,
                OauthFlows.__table__.c.user_id == user_id,
            )
        ).mappings().first()
        return dict(row) if row else None

    def update_oauth_flow(self, flow_id: str, token: str, resources: str) -> None:
        self.session.execute(
            update(OauthFlows.__table__)
            .where(OauthFlows.__table__.c.id == flow_id)
            .values(token=token, resources=resources)
        )

    def setting(self, key: str) -> str:
        row = self.session.get(Setting, key)
        return row.value if row else ""

    def set_setting(self, key: str, value: str) -> None:
        row = self.session.get(Setting, key)
        if row is None:
            self.session.add(Setting(key=key, value=value))
        else:
            row.value = value
