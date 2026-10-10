from __future__ import annotations

from typing import Any


def create_app(*args: Any, **kwargs: Any):
    """Create the application through the compatibility adapter.

    The adapter keeps the public API stable while the legacy Flask routes are
    extracted into ``api/routes`` in subsequent migration steps.
    """

    from media_server_manager_web.app import create_app as legacy_create_app

    return legacy_create_app(*args, **kwargs)
