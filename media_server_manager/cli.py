"""Compatibility CLI wrapper with package semantics for source checkouts."""

from pathlib import Path

# A checkout historically contained ``media_server_manager/cli.py`` while the
# canonical package now uses ``src/media_server_manager/cli/``.  Exposing the
# canonical directory as this module's package path keeps both
# ``media_server_manager.cli`` and ``media_server_manager.cli.migrate`` valid.
_canonical_cli = Path(__file__).resolve().parent.parent / "src" / "media_server_manager" / "cli"
__path__ = [str(_canonical_cli)]

from media_server_manager.cli.main import main, run_cli  # noqa: E402

__all__ = ["main", "run_cli"]
