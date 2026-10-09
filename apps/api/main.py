from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from apps.api.config import get_settings
from apps.api.middleware.request_context import RequestContextMiddleware
from apps.api.routers import (
    automation,
    catalog,
    diagnostics,
    jobs,
    media_libraries,
    members,
    overview,
    servers,
    tenants,
    webhooks,
)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version="3.0.0", docs_url="/docs", redoc_url="/redoc")
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

    @app.exception_handler(Exception)
    async def handle_exception(request: Request, exc: Exception):
        if settings.environment == "development":
            message = str(exc)
        else:
            message = "internal server error"
        return JSONResponse(status_code=500, content={"error": {"code": "internal_error", "message": message, "details": {}}, "request_id": getattr(request.state, "request_id", "")})

    @app.get("/healthz", tags=["system"])
    def healthz():
        return {"status": "ok", "service": "api", "version": "3.0.0"}

    app.include_router(tenants.router, prefix=settings.api_prefix)
    app.include_router(servers.router, prefix=settings.api_prefix)
    app.include_router(jobs.router, prefix=settings.api_prefix)
    for router in (members.router, media_libraries.router, automation.router, catalog.router, diagnostics.router, overview.router, webhooks.router):
        app.include_router(router, prefix=settings.api_prefix)
    return app


app = create_app()
