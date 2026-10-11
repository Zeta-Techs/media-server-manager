"""Job handler modules.

Each module exposes a stable handler type while the legacy executor is being
retired.  The registry remains the single place where worker job types are
bound to implementations.
"""

from .base import ApplicationTask

__all__ = ["ApplicationTask"]
