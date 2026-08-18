"""Auth, webhook signing, and the production config guards."""

from __future__ import annotations

import time

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.security import SlidingWindowLimiter, sign_payload, verify_signature

from .conftest import TEST_KEY


class TestAuth:
    def test_health_is_open(self, client):
        assert client.get("/health").status_code == 200

    def test_protected_route_requires_a_key(self, client):
        assert client.get("/v1/models").status_code == 401

    def test_bearer_token_accepted(self, client, auth):
        assert client.get("/v1/models", headers=auth).status_code == 200

    def test_x_api_key_header_accepted(self, client):
        response = client.get("/v1/models", headers={"X-API-Key": TEST_KEY})
        assert response.status_code == 200

    def test_wrong_key_rejected(self, client):
        response = client.get("/v1/models", headers={"Authorization": "Bearer nope"})
        assert response.status_code == 401

    def test_second_key_works_independently(self, client):
        response = client.get(
            "/v1/models",
            headers={"Authorization": "Bearer drk_second_key_bbbbbbbbbbbb"},
        )
        assert response.status_code == 200


class TestProductionGuards:
    """The reference deployment could boot wide open. This one refuses to."""

    def test_production_requires_api_keys(self):
        with pytest.raises(ValidationError, match="API_KEYS"):
            Settings(
                environment="production",
                api_keys="",
                webhook_secret="x",
                cors_origins="https://app.example.com",
            )

    def test_production_rejects_wildcard_cors(self):
        with pytest.raises(ValidationError, match="CORS_ORIGINS"):
            Settings(
                environment="production",
                api_keys="k",
                webhook_secret="x",
                cors_origins="*",
            )

    def test_production_rejects_disabled_auth(self):
        with pytest.raises(ValidationError, match="AUTH_DISABLED"):
            Settings(
                environment="production",
                api_keys="k",
                auth_disabled=True,
                webhook_secret="x",
                cors_origins="https://app.example.com",
            )

    def test_production_requires_webhook_secret(self):
        with pytest.raises(ValidationError, match="WEBHOOK_SECRET"):
            Settings(
                environment="production",
                api_keys="k",
                webhook_secret="",
                cors_origins="https://app.example.com",
            )

    def test_development_stays_permissive(self):
        assert Settings(environment="development", api_keys="").environment == "development"


class TestWebhookSignature:
    def test_roundtrip(self):
        body = b'{"event":"research.succeeded"}'
        header, _ = sign_payload("secret", body)
        assert verify_signature("secret", body, header)

    def test_wrong_secret_fails(self):
        body = b"{}"
        header, _ = sign_payload("secret", body)
        assert not verify_signature("other", body, header)

    def test_tampered_body_fails(self):
        header, _ = sign_payload("secret", b'{"amount":1}')
        assert not verify_signature("secret", b'{"amount":999}', header)

    def test_replayed_old_signature_fails(self):
        body = b"{}"
        stale = int(time.time()) - 4000
        header, _ = sign_payload("secret", body, timestamp=stale)
        # correctly signed, but far outside the tolerance window
        assert not verify_signature("secret", body, header, tolerance_seconds=300)

    def test_malformed_header_fails(self):
        assert not verify_signature("secret", b"{}", "garbage")


class TestRateLimiter:
    def test_allows_up_to_limit_then_blocks(self):
        limiter = SlidingWindowLimiter(limit=3, window_seconds=60)
        assert all(limiter.check("caller")[0] for _ in range(3))
        allowed, _, retry_after = limiter.check("caller")
        assert not allowed
        assert retry_after > 0

    def test_callers_are_isolated(self):
        limiter = SlidingWindowLimiter(limit=1, window_seconds=60)
        assert limiter.check("a")[0]
        assert limiter.check("b")[0]
        assert not limiter.check("a")[0]
