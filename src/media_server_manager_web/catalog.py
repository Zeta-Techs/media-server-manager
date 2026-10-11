"""Legacy import wrapper for the canonical implementation."""
import sys

from media_server_manager.application.catalog_sync.operations import __name__ as _canonical_name

_implementation = sys.modules[_canonical_name]
sys.modules[__name__] = _implementation
