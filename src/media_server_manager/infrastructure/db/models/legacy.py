from __future__ import annotations

from sqlalchemy import Engine
from sqlalchemy.ext.automap import automap_base

LegacyBase = automap_base()


def reflect_legacy_schema(engine: Engine):
    """Reflect remaining tables while typed domain models are extracted.

    This is intentionally transitional: it gives repositories access to all
    existing tables without duplicating 47 hand-written definitions. New
    features should use typed models and promote reflected tables gradually.
    """

    LegacyBase.prepare(autoload_with=engine, reflect=True)
    return LegacyBase
