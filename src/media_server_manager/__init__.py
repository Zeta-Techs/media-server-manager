"""Media Server Manager application package.

The source package is intentionally independent from the legacy compatibility
packages while the application is migrated in stages.  The public factory is
kept here so new integrations do not need to know about the old Flask module.
"""

def create_app(*args, **kwargs):
    """Lazily create the Flask application."""

    from .api.app_factory import create_app as factory

    return factory(*args, **kwargs)

__all__ = ["create_app"]
