from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.schemas import (  # noqa: E402
    Event,
    EventType,
    ResearchResult,
    ResearchStage,
    Source,
)

TEST_KEY = "drk_test_key_aaaaaaaaaaaaaaaaaaaa"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment="development",
        api_keys=f"{TEST_KEY},drk_second_key_bbbbbbbbbbbb",
        anthropic_api_key="sk-ant-test",
        openrouter_api_key="sk-or-test",
        moonshot_api_key="sk-moon-test",
        tavily_api_key="tvly-test",
        webhook_secret="whsec_test_secret",
        cors_origins="http://localhost:3000",
        rate_limit_requests=5,
        rate_limit_window_seconds=60,
    )


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_KEY}"}


@pytest.fixture
def stub_engine(app):
    """Replace the graph with a scripted run -- no network, no model calls."""

    class StubEngine:
        def __init__(self):
            self.calls: list[str] = []
            self.delay = 0.0
            self.fail_with: Exception | None = None

        def resolve(self, options):
            from app.providers import get_provider

            spec = get_provider(options.provider or "anthropic")
            return spec, options.model or spec.default_model, "tavily"

        async def stream(self, query, options, job_id, cancel_event=None, prior_context=None):
            self.calls.append(query)
            if self.fail_with:
                raise self.fail_with
            yield Event(
                type=EventType.STAGE_START,
                job_id=job_id,
                stage=ResearchStage.RESEARCH_BRIEF,
                content="brief",
            ), None
            if self.delay:
                await asyncio.sleep(self.delay)
            result = ResearchResult(
                report_markdown="# Findings\n\nBody.",
                sources=[Source(url="https://example.com", title="Example")],
            )
            yield Event(
                type=EventType.JOB_SUCCEEDED,
                job_id=job_id,
                stage=ResearchStage.COMPLETE,
            ), result

    stub = StubEngine()
    app.state.engine = stub
    app.state.jobs.engine = stub
    return stub
