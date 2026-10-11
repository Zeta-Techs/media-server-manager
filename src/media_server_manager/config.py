from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _path_from_env(*names: str) -> Path | None:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return Path(value).expanduser()
    return None


def data_dir() -> Path:
    """Return the runtime data directory.

    ``MSM_CONFIG_DIR`` and ``MSM_CONFIG_PATH`` are accepted during the
    migration window so existing deployments can be moved without changing
    every process at once.
    """

    return _path_from_env("MSM_DATA_DIR", "MSM_CONFIG_DIR", "MSM_CONFIG_PATH") or PROJECT_ROOT / "data"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_url: str
    web_host: str
    web_port: int
    web_threads: int
    timezone: str
    cookie_secure: bool
    encryption_key: str
    environment: str = "development"
    item_workers: int = 4
    worker_concurrency: int = 2
    job_retention_days: int = 90
    webhook_retention_days: int = 30

    @classmethod
    def from_env(cls) -> "Settings":
        root = data_dir()
        database = os.environ.get("MSM_DATABASE_URL", "").strip()
        if not database:
            database = f"sqlite:///{(root / 'media_server_manager.sqlite3').as_posix()}"
        return cls(
            data_dir=root,
            database_url=database,
            web_host=os.environ.get("MSM_WEB_HOST", "0.0.0.0"),
            web_port=int(os.environ.get("MSM_WEB_PORT", "8088")),
            web_threads=max(2, int(os.environ.get("MSM_WEB_THREADS", "8"))),
            timezone=os.environ.get("MSM_TIMEZONE", "Asia/Shanghai"),
            cookie_secure=os.environ.get("MSM_COOKIE_SECURE", "0") == "1",
            encryption_key=os.environ.get("MSM_LOCAL_ENCRYPTION_KEY", ""),
            environment=os.environ.get("MSM_ENV", "development").strip().lower(),
            item_workers=max(1, int(os.environ.get("MSM_ITEM_WORKERS", "4"))),
            worker_concurrency=max(1, int(os.environ.get("MSM_WORKER_CONCURRENCY", "2"))),
            job_retention_days=max(1, int(os.environ.get("MSM_JOB_RETENTION_DAYS", "90"))),
            webhook_retention_days=max(1, int(os.environ.get("MSM_WEBHOOK_RETENTION_DAYS", "30"))),
        )

    @property
    def database_path(self) -> Path:
        if not self.database_url.startswith("sqlite:///"):
            raise ValueError("database_path 仅适用于 SQLite 数据库")
        return Path(self.database_url.removeprefix("sqlite:///"))

    def ensure_directories(self) -> None:
        if self.environment in {"production", "prod"} and not self.encryption_key.strip():
            raise RuntimeError("生产环境必须配置 MSM_LOCAL_ENCRYPTION_KEY")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "cache" / "tmdb").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "backups").mkdir(parents=True, exist_ok=True)
        # SQLite contains credentials and account data.  Tighten permissions
        # whenever an existing database is opened; newly created files are
        # created by SQLite under the process umask and are tightened by the
        # database bootstrap immediately afterwards.
        if self.database_url.startswith("sqlite:///"):
            database = self.database_path
            database.parent.mkdir(parents=True, exist_ok=True)
            if database.exists():
                database.chmod(0o600)


settings = Settings.from_env()
