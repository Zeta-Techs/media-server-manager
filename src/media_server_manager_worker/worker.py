import sys

from media_server_manager.worker import runtime as _implementation

sys.modules[__name__] = _implementation
