from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

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
