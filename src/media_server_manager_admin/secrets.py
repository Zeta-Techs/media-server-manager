import sys

try:
    from media_server_manager.cli import secrets as _implementation  # type: ignore[attr-defined]
except (ImportError, ModuleNotFoundError):
    from src.media_server_manager.cli import secrets as _implementation

sys.modules[__name__] = _implementation
