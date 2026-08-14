import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .api import alerts, api_keys, auth, cages, detections, ingest, mouse, readings, schedule, system
from .config import get_settings
from .db import Base, SessionLocal, checkpoint_wal, engine
from .limiter import limiter
from .logging_config import configure_logging
from .middleware import SecurityHeadersMiddleware
from .seed import seed_initial_data
from .services.aggregator import aggregator_loop
from .services.scheduler import scheduler_loop
from .ws import router as ws_router

settings = get_settings()
configure_logging(settings.log_level)
log = logging.getLogger(__name__)


def _ensure_columns() -> None:
    """Add columns introduced by newer code to existing SQLite tables.

    Idempotent — checks PRAGMA first so re-running on an already-migrated DB
    is a no-op. Add new entries here whenever a model gains a column.
    """
    from sqlalchemy import text
    pending = [
        # (table, column, DDL)
        ("schedule_windows", "timezone",
         "ALTER TABLE schedule_windows ADD COLUMN timezone VARCHAR(64) NOT NULL DEFAULT 'Europe/Brussels'"),
    ]
    with engine.begin() as conn:
        for table, column, ddl in pending:
            cols = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
            if column not in cols:
                log.info({"event": "migrate.add_column", "table": table, "column": column})
                conn.execute(text(ddl))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Production safety: refuse to boot with the default dev JWT secret.
    if not settings.is_dev and settings.jwt_secret.startswith("dev-only-secret"):
        raise RuntimeError(
            "Refusing to boot in production with the default dev JWT secret. "
            "Set JWT_SECRET to a 32+ char random value."
        )

    log.info({"event": "boot", "env": settings.env, "version": __version__})
    checkpoint_wal()  # Clear any stale WAL lock left by a prior crash
    Base.metadata.create_all(bind=engine)
    # Lightweight forward-only migrations for SQLite (no Alembic in this repo).
    # `create_all` only creates missing tables; column additions need ALTER.
    _ensure_columns()
    db = SessionLocal()
    try:
        seed_initial_data(db)
    finally:
        db.close()

    task = asyncio.create_task(aggregator_loop())
    sched_task = asyncio.create_task(scheduler_loop())
    yield
    for t in (task, sched_task):
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
    log.info({"event": "shutdown"})


app = FastAPI(
    title="IVC Cage Backend",
    version=__version__,
    description="Backend for the Integrated Intelligent Precision Metabolic & Behavioural IVC Cage.",
    lifespan=lifespan,
)

# --- Security middleware stack ---
# Note: TrustedHostMiddleware was tried here, but Railway's healthcheck uses
# an internal Host header that doesn't match the public allowlist, killing the
# deploy. Host validation is already enforced by Railway's edge router before
# traffic reaches us, so it would only add value if we self-hosted.

app.add_middleware(SecurityHeadersMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept"],
    max_age=600,
)

# Rate limiter — exposed on app.state for use in route decorators.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(_request: Request, exc: StarletteHTTPException):
    detail = exc.detail
    if isinstance(detail, dict):
        code = detail.get("code", "ERROR")
        message = detail.get("message", str(detail))
    else:
        code = "ERROR"
        message = str(detail)
    return JSONResponse(status_code=exc.status_code, content={"error": {"code": code, "message": message}})


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_request: Request, _exc: RequestValidationError):
    # Don't echo bodies — they may contain credentials or sensitive input.
    return JSONResponse(
        status_code=422,
        content={"error": {"code": "VALIDATION_FAILED", "message": "Request validation failed."}},
    )


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok", "version": __version__}


@app.get("/api/v1/meta", tags=["meta"])
def meta():
    return {
        "version": __version__,
        "env": settings.env,
        "feature_flags": {"alerts": True, "reports": True, "ml": True},
    }


app.include_router(auth.router)
app.include_router(cages.router)
app.include_router(ingest.router)
app.include_router(readings.router)
app.include_router(alerts.router)
app.include_router(schedule.router)
app.include_router(system.router)
app.include_router(detections.router)
app.include_router(api_keys.router)
app.include_router(mouse.router)
app.include_router(ws_router)

# Serve the built React frontend when the static/ folder is present.
# On Railway (no static/ dir) this is silently skipped.
import os
from pathlib import Path as _Path
from starlette.responses import FileResponse as _FileResponse, Response as _Response

_STATIC = _Path(__file__).parent.parent / "static"
if _STATIC.is_dir():
    app.mount("/assets", StaticFiles(directory=str(_STATIC / "assets")), name="assets")

    # Files in public/ that must be served with their *actual* content (not the
    # SPA fallback) and the correct MIME type. Order matters for sub-paths like
    # /.well-known/security.txt.
    _ROOT_FILES = {
        "favicon.svg":            "image/svg+xml",
        "favicon-mono.svg":       "image/svg+xml",
        "apple-touch-icon.svg":   "image/svg+xml",
        "manifest.webmanifest":   "application/manifest+json",
        "robots.txt":             "text/plain; charset=utf-8",
        ".well-known/security.txt": "text/plain; charset=utf-8",
    }

    # Cache index.html content so the SPA fallback is an in-memory Response —
    # NOT a FileResponse. The FileResponse-per-request was the root cause of
    # hung GETs: under the CF tunnel, BaseHTTPMiddleware + FileResponse stalls
    # on streamed responses (https://github.com/encode/starlette/issues/919),
    # exhausting the worker pool so other routes hang too.
    #
    # BUT: caching forever meant a frontend rebuild (new hashed JS bundle in a
    # fresh index.html) wasn't served until the backend restarted — and
    # frontend-only deploys don't restart the backend. So we now re-read the
    # file whenever its mtime changes. A bare os.stat() is cheap and does NOT
    # have the FileResponse streaming-deadlock problem, so the hot path stays safe.
    _INDEX_PATH = _STATIC / "index.html"
    _index_cache: dict = {"mtime": 0.0, "bytes": b""}

    def _index_bytes() -> bytes:
        try:
            mtime = _INDEX_PATH.stat().st_mtime
        except OSError:
            return _index_cache["bytes"]
        if mtime != _index_cache["mtime"]:
            try:
                _index_cache["bytes"] = _INDEX_PATH.read_bytes()
                _index_cache["mtime"] = mtime
                log.info({"event": "spa.index_reloaded", "mtime": mtime})
            except OSError:
                pass
        return _index_cache["bytes"]

    # Warm the cache at startup.
    _index_bytes()

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa_fallback(full_path: str):
        """Serve known public assets verbatim; everything else → index.html so
        React Router can take over routing (deep links, refresh)."""
        # Block obvious traversal attempts
        if ".." in full_path:
            return _Response(status_code=404)

        # Defence-in-depth: API / WS / health should *already* have matched a
        # specific route by this point. If they didn't, returning index.html
        # would mask the bug AND can stall workers. Return 404 fast instead.
        if (full_path.startswith(("api/", "ws/", "ws"))
                or full_path == "health"
                or full_path == "openapi.json"):
            return _Response(status_code=404)

        if full_path in _ROOT_FILES:
            file_path = _STATIC / full_path
            if file_path.is_file():
                return _FileResponse(str(file_path), media_type=_ROOT_FILES[full_path])

        # index.html — re-read only when the file changed (cheap stat on hot path).
        body = _index_bytes()
        if not body:
            return _Response(status_code=503, content=b"Frontend not built yet")
        # no-cache so browsers always fetch the latest index → newest bundle hash
        return _Response(content=body, media_type="text/html",
                         headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
