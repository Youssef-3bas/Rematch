import logging
from typing import Literal

from fastapi import APIRouter, Request, Response, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app import __version__
from app.db.session import get_engine
from app.schemas.health import HealthResponse, ProbeHealthResponse

logger = logging.getLogger(__name__)

# Platform probes intentionally live outside /api/v1, whose router enforces bearer auth.
probe_router = APIRouter(tags=["platform health"])
router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        service="rematch-backend",
        version=__version__,
    )


@probe_router.get("/health/startup", include_in_schema=False)
def startup_probe(request: Request, response: Response) -> ProbeHealthResponse:
    """Report whether lifespan initialization and its database check completed."""
    ready = bool(getattr(request.app.state, "startup_complete", False))
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ProbeHealthResponse(status="ok" if ready else "not_ready", check="startup")


@probe_router.get("/health/live", include_in_schema=False)
def liveness_probe() -> ProbeHealthResponse:
    """Report that the ASGI process is serving requests; no downstream checks."""
    return ProbeHealthResponse(status="ok", check="liveness")


@probe_router.get("/health/ready", include_in_schema=False)
def readiness_probe(response: Response) -> ProbeHealthResponse:
    """Report readiness only while the process and its configured database respond."""
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        # Do not log the exception text: driver errors can contain connection details.
        logger.warning(
            "Readiness check failed during database query (%s)",
            type(exc).__name__,
        )
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ProbeHealthResponse(status="not_ready", check="readiness")
    return ProbeHealthResponse(status="ok", check="readiness")
