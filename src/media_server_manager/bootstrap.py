from __future__ import annotations

from .config import Settings, settings


def initialize(app_settings: Settings = settings) -> Settings:
    """Prepare runtime directories and return immutable application settings."""

    app_settings.ensure_directories()
    return app_settings
