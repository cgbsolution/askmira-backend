"""AskMira API entry point."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.v1 import api_router
from app.api.v1.routers import scim as scim_router
from app.core.config import settings
from app.db.session import engine

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("askmira")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Fail loudly at boot if the database is unreachable.

    Better than surfacing it as a 500 on the first request a customer makes.
    """
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        logger.info("Database connection established")
    except SQLAlchemyError:
        logger.exception("Database unreachable at startup")
        if settings.is_production:
            raise
    yield
    await engine.dispose()


app = FastAPI(
    title=f"{settings.APP_NAME} API",
    description=(
        "Culture-analytics-first HRMS for the Indian mid-market. "
        "Auth is self-issued JWT; Supabase supplies Postgres only."
    ),
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if not settings.is_production else None,
    redoc_url="/redoc" if not settings.is_production else None,
    openapi_url="/openapi.json" if not settings.is_production else None,
)

class SecurityHeadersMiddleware:
    """Hardening headers on every response.

    A pure ASGI middleware rather than `BaseHTTPMiddleware`, so the server-sent
    notification stream is not buffered.

    The API serves JSON and files, never pages, so its CSP forbids everything
    and it may not be framed. The interactive docs (development only) load
    scripts from a CDN and are left without a CSP.
    """

    HEADERS: tuple[tuple[bytes, bytes], ...] = (
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"referrer-policy", b"no-referrer"),
        (b"cross-origin-opener-policy", b"same-origin"),
        (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
    )
    DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")
    API_CSP = b"default-src 'none'; frame-ancestors 'none'"

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")

        async def send_with_headers(message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                for name, value in self.HEADERS:
                    if name not in present:
                        headers.append((name, value))
                if not path.startswith(self.DOCS_PATHS):
                    if b"content-security-policy" not in present:
                        headers.append((b"content-security-policy", self.API_CSP))
                    if b"cache-control" not in present:
                        # Personal data must not sit in a shared or browser cache.
                        headers.append((b"cache-control", b"no-store"))
                if settings.is_production:
                    headers.append(
                        (b"strict-transport-security", b"max-age=63072000; includeSubDomains")
                    )
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-CSRF-Token", "X-Auth-Transport"],
    expose_headers=["Content-Disposition", "Retry-After"],
)


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Flatten Pydantic errors into one readable line.

    The frontend shows `detail` directly in a toast, so it has to be a sentence
    rather than a nested error tree.
    """
    problems = [
        f"{'.'.join(str(part) for part in err['loc'][1:])}: {err['msg']}" for err in exc.errors()
    ]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "; ".join(problems) or "Invalid request"},
    )


@app.exception_handler(SQLAlchemyError)
async def database_handler(request: Request, exc: SQLAlchemyError) -> JSONResponse:
    """Never leak SQL or schema detail to a client."""
    logger.exception("Database error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "A database error occurred. Please try again."},
    )


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    """Liveness probe. Does not touch the database."""
    return {"status": "ok", "app": settings.APP_NAME, "environment": settings.ENVIRONMENT}


@app.get("/health/db", tags=["meta"])
async def health_db() -> dict[str, str]:
    """Readiness probe. Confirms the database actually answers."""
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return {"status": "ok", "database": "reachable"}


app.include_router(api_router, prefix=settings.API_V1_PREFIX)

# SCIM lives at /scim/v2, outside the versioned prefix: clients expect that
# exact path and several will not let you configure another.
app.include_router(scim_router.router)
