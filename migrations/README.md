# Media Server Manager migrations

Schema changes are applied with Alembic:

```text
alembic upgrade head
```

The first revision bootstraps the legacy schema through the compatibility
initializer. New revisions must use SQLAlchemy/Alembic operations and must not
append more conditional DDL to the runtime database module.

During the extraction period, `infrastructure.db.models.legacy` can reflect
tables that do not yet have a typed model. This keeps repository access inside
the database layer while each domain is migrated incrementally.
