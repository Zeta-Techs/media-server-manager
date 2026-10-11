"""Development checkout shim for the canonical ``src`` package.

The repository keeps this tiny shim so ``python`` launched from the checkout
continues to work before an editable install.  Installed distributions only
contain ``src/media_server_manager``.
"""

from pathlib import Path

_canonical = Path(__file__).resolve().parent.parent / "src" / "media_server_manager"
if _canonical.is_dir():
    __path__.append(str(_canonical))


def create_app(*args, **kwargs):
    from .api.app_factory import create_app as factory

    return factory(*args, **kwargs)


__all__ = ["create_app"]
