"""HTTP surface: /v1/research (async jobs), SSE stream, models, health."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from . import __version__
from .config import Settings, get_settings
from .jobs import JobManager, JobNotFound
from .providers import (
    PROVIDERS,
    ProviderNotConfiguredError,
    UnknownProviderError,
    configured_providers,
)
from .schemas import (
    HealthResponse,
    Job,
    JobAccepted,
    JobStatus,
    ProviderInfo,
    ResearchRequest,
)
from .security import CallerId, RateLimitedCaller

logger = logging.getLogger(__name__)

router = APIRouter()
SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_jobs(request: Request) -> JobManager:
    return request.app.state.jobs


JobsDep = Annotated[JobManager, Depends(get_jobs)]


# --------------------------------------------------------------------------
# health -- unauthenticated so orchestrators can probe it
# --------------------------------------------------------------------------

@router.get("/health", response_model=HealthResponse, tags=["ops"])
@router.head("/health", include_in_schema=False)
async def health(settings: SettingsDep) -> HealthResponse:
    configured = [s.id for s in configured_providers(settings)]
    return HealthResponse(
        status="ok" if configured else "degraded",
        service=settings.service_name,
        version=__version__,
        environment=settings.environment,
        providers_configured=configured,
        search_configured=bool(settings.tavily_api_key),
        job_backend=settings.job_backend,
    )


@router.get("/", include_in_schema=False)
async def root(settings: SettingsDep) -> dict:
    return {
        "service": settings.service_name,
        "version": __version__,
        "docs": "/docs",
        "health": "/health",
    }


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------

@router.get("/v1/models", response_model=list[ProviderInfo], tags=["models"])
async def list_models(settings: SettingsDep, caller: CallerId) -> list[ProviderInfo]:
    """Which providers this deployment can actually run, and on what."""
    return [
        ProviderInfo(
            id=spec.id,
            label=spec.label,
            default_model=spec.default_model,
            context_window=spec.context_window,
            configured=bool(getattr(settings, spec.settings_key_attr, "")),
            native_search=list(spec.native_search),
            notes=spec.notes,
        )
        for spec in PROVIDERS
    ]


# --------------------------------------------------------------------------
# research
# --------------------------------------------------------------------------

@router.post(
    "/v1/research",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["research"],
)
async def create_research(
    payload: ResearchRequest,
    request: Request,
    response: Response,
    jobs: JobsDep,
    settings: SettingsDep,
    caller: RateLimitedCaller,
) -> JobAccepted:
    """Start a research run.

    Returns 202 immediately -- a run takes 30-120s, far too long to hold a
    request open. Track it by polling `poll_url`, streaming `events_url`, or
    supplying `callback_url` for a signed webhook.
    """
    try:
        job = await jobs.submit(payload)
    except UnknownProviderError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except ProviderNotConfiguredError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    if payload.callback_url and not settings.webhook_secret:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "callback_url was supplied but WEBHOOK_SECRET is not configured; "
            "the service will not send unsigned webhooks.",
        )

    base = str(request.base_url).rstrip("/")
    response.headers["Location"] = f"{base}/v1/research/{job.id}"
    return JobAccepted(
        id=job.id,
        status=job.status,
        created_at=job.created_at,
        poll_url=f"{base}/v1/research/{job.id}",
        events_url=f"{base}/v1/research/{job.id}/events",
    )


@router.get("/v1/research", response_model=list[Job], tags=["research"])
async def list_research(
    jobs: JobsDep,
    caller: CallerId,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    job_status: Annotated[JobStatus | None, Query(alias="status")] = None,
) -> list[Job]:
    return await jobs.list(limit=limit, status=job_status)


@router.get("/v1/research/{job_id}", response_model=Job, tags=["research"])
async def get_research(job_id: str, jobs: JobsDep, caller: CallerId) -> Job:
    try:
        return await jobs.get(job_id)
    except JobNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No job {job_id!r}") from None


@router.delete("/v1/research/{job_id}", response_model=Job, tags=["research"])
async def cancel_research(job_id: str, jobs: JobsDep, caller: CallerId) -> Job:
    try:
        return await jobs.cancel(job_id)
    except JobNotFound:  # noqa: PERF203 - distinct 404 body
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No job {job_id!r}") from None


@router.get("/v1/research/{job_id}/events", tags=["research"])
async def stream_events(job_id: str, jobs: JobsDep, caller: CallerId) -> StreamingResponse:
    """Server-Sent Events for a run.

    Subscribing late is fine: the backlog replays first, so you still see the
    whole run. Heartbeats every 15s keep intermediaries from dropping the
    connection during a long model call.
    """
    try:
        await jobs.get(job_id)
    except JobNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No job {job_id!r}") from None

    async def generator():
        async for event in jobs.events(job_id):
            yield event.to_sse()

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # nginx must not buffer an event stream
        },
    )
