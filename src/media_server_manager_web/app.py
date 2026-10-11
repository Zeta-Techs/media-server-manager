"""Legacy import wrapper for the canonical Flask application."""
import sys

from media_server_manager.api import app_factory as _implementation

sys.modules[__name__] = _implementation
