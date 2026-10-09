# Modular deployment

`docker compose -f deploy/compose/docker-compose.modular.yml up --build` starts
PostgreSQL, Redis, the FastAPI API and Celery workers. Set `POSTGRES_PASSWORD`
and the `MSM_OIDC_*` variables before production use. Run `alembic upgrade head`
against the API image before creating tenants.
