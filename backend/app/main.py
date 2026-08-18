"""Application factory, middleware, and lifespan."""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import __version__
from .config import Settings, get_settings
from .jobs import JobManager
from .logging_config import configure_logging
from .providers import configured_providers
from .research import DeepResearchEngine
from .routes import router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    configure_logging(settings)

    engine = DeepResearchEngine(settings)
    app.state.engine = engine
    app.state.jobs = await JobManager.create(settings, engine)

    configured = [s.id for s in configured_providers(settings)]
    if configured:
        logger.info("providers ready: %s", ", ".join(configured))
    else:
        logger.warning(
            "no provider credentials found -- /v1/research will return 503 until "
            "one of ANTHROPIC_API_KEY / OPENAI_API_KEY / MOONSHOT_API_KEY / "
            "OPENROUTER_API_KEY is set"
        )
    if not settings.tavily_api_key:
        logger.warning("TAVILY_API_KEY unset -- falling back to provider-native search")

    try:
        yield
    finally:
        logger.info("draining in-flight jobs")
        await app.state.jobs.shutdown()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    app = FastAPI(
        title="Deep Research API",
        description=(
            "Production deep-research agent service. Submit a query, get a job id, "
            "receive a signed webhook or stream events. Provider-agnostic."
        ),
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    app.state.settings = settings

    # Route every `Depends(get_settings)` to *this* app's settings. Without the
    # override the dependency resolves the lru_cached module-level singleton,
    # so an app built with explicit settings would silently authenticate
    # against whatever the ambient environment happened to hold.
    app.dependency_overrides[get_settings] = lambda: settings

    # Explicit origins. Never "*" with credentials -- browsers reject that pair
    # outright, and it would expose the API to any page on the internet.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-API-Key"],
        expose_headers=["Location", "X-Request-Id"],
        max_age=600,
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex[:16]
        request.state.request_id = request_id
        started = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "unhandled error", extra={"request_id": request_id, "path": request.url.path}
            )
            return JSONResponse(
                status_code=500,
                content={"detail": "Internal server error", "request_id": request_id},
                headers={"X-Request-Id": request_id},
            )
        elapsed_ms = (time.monotonic() - started) * 1000
        response.headers["X-Request-Id"] = request_id
        # event streams are long-lived; timing them is meaningless noise
        if not request.url.path.endswith("/events"):
            logger.info(
                "%s %s -> %d (%.1fms)",
                request.method,
                request.url.path,
                response.status_code,
                elapsed_ms,
                extra={"request_id": request_id},
            )
        return response

    app.include_router(router)
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    s = get_settings()
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=s.port,
        reload=s.environment == "development",
        log_config=None,
    )
