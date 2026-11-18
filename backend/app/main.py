"""FastAPI application entry point.

Run locally:  cd backend && uv run uvicorn app.main:app --reload
"""

from pathlib import Path

import structlog
from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import admin, sources
from app.auth import router as auth_router
from app.aws import check_aws
from app.db import check_database

log = structlog.get_logger()

app = FastAPI(title="Operations Copilot", version="0.1.0")
app.include_router(auth_router)
app.include_router(sources.router)
app.include_router(admin.router)


@app.get("/health")
async def health() -> JSONResponse:
    """Report whether the API can reach its dependencies.

    Returns 200 when all dependencies are reachable, otherwise 503. A deploy script or load
    balancer can then tell "process running" apart from "able to serve".
    The blocking DB and boto3 calls run in a thread pool so they don't stall the event loop.
    """
    checks: dict[str, dict[str, str]] = {}
    for name, check in (("database", check_database), ("aws", check_aws)):
        try:
            checks[name] = await run_in_threadpool(check)
        except Exception as exc:  # report any failure instead of crashing the health endpoint
            log.warning("health_check_failed", dependency=name, error=str(exc))
            checks[name] = {"status": "error", "error": type(exc).__name__}
    healthy = all(c["status"] == "ok" for c in checks.values())
    body = {"status": "ok" if healthy else "degraded", "checks": checks}
    return JSONResponse(body, status_code=200 if healthy else 503)


# Serve the built React app (frontend/dist) from the same origin as the API, when present.
# Same origin means no CORS configuration, and login cookies just work.
# Must be mounted last, so API routes take precedence over the catch-all static mount.
_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _DIST.is_dir():
    app.mount("/", StaticFiles(directory=_DIST, html=True), name="spa")
