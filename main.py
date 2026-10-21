import os
from contextlib import asynccontextmanager
import logging
from time import perf_counter
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import COOKIE_SECURE, SECRET_KEY
from app.routes import router
from app.neo4j_client import initialize_neo4j, neo4j_status, shutdown_neo4j
from app.health import check_readiness
from app.observability import increment, log_event, prometheus_text
from app.realtime import chat_manager

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize Neo4j at startup and close it during application shutdown."""
    try:
        initialize_neo4j()
    except Exception:
        # PostgreSQL remains the primary store; a graph outage must not prevent
        # the existing application from starting.
        logger.exception("Neo4j initialization failed; graph features are unavailable")
    await chat_manager.start()
    try:
        yield
    finally:
        await chat_manager.stop()
        shutdown_neo4j()

app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def request_observability(request: Request, call_next):
    """Record request count, failures, latency, method, path, and status."""
    started = perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    except Exception:
        raise
    finally:
        elapsed_ms = round((perf_counter() - started) * 1000, 2)
        increment("http_requests_total")
        if status_code >= 500:
            increment("http_request_failures_total")
        log_event(
            "http_request",
            method=request.method,
            path=request.url.path,
            status_code=status_code,
            latency_ms=elapsed_ms,
        )


app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    same_site="lax",
    https_only=COOKIE_SECURE,
)

app.mount("/static", StaticFiles(directory="static"), name="static")
os.makedirs("uploads", exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

app.include_router(router)


@app.get("/health/neo4j")
def neo4j_health():
    """Report whether the configured Neo4j driver can reach its database."""
    connected, _error = neo4j_status()
    if connected:
        return {"status": "ok", "service": "neo4j"}
    return JSONResponse(
        status_code=503,
        content={"status": "unavailable", "service": "neo4j", "detail": "Neo4j unavailable"},
    )


@app.get("/health/live")
def liveness_health():
    """Return a lightweight liveness response for the web process."""
    return {"status": "ok", "service": "instagram_clone"}


@app.get("/health/ready")
def readiness_health():
    """Report readiness only when PostgreSQL and Neo4j are both available."""
    readiness = check_readiness()
    content = {
        "status": "ok" if readiness.ready else "unavailable",
        "service": "instagram_clone",
        "dependencies": {
            "postgres": "ok" if readiness.postgres else "unavailable",
            "neo4j": "ok" if readiness.neo4j else "unavailable",
        },
    }
    return JSONResponse(status_code=200 if readiness.ready else 503, content=content)


@app.get("/metrics", response_class=PlainTextResponse)
def metrics():
    """Expose process-local operational counters as Prometheus text."""
    return prometheus_text()
"""FastAPI application entry point, middleware, static files, and health routes."""
