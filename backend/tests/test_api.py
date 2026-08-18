"""API contract tests. The research engine is stubbed so these stay fast and offline."""

from __future__ import annotations

import pytest


def _wait_for(client, job_id, auth, target="succeeded", tries=100):
    for _ in range(tries):
        body = client.get(f"/v1/research/{job_id}", headers=auth).json()
        if body["status"] == target:
            return body
        if body["status"] in ("failed", "cancelled") and target == "succeeded":
            pytest.fail(f"job ended as {body['status']}: {body.get('error')}")
    pytest.fail(f"job never reached {target}")


class TestHealth:
    def test_reports_configured_providers(self, client):
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert set(body["providers_configured"]) == {"anthropic", "moonshot", "openrouter"}
        assert body["search_configured"] is True

    def test_degraded_without_credentials(self, app, settings):
        from fastapi.testclient import TestClient

        settings.anthropic_api_key = ""
        settings.openrouter_api_key = ""
        settings.moonshot_api_key = ""
        with TestClient(app) as c:
            assert c.get("/health").json()["status"] == "degraded"


class TestModels:
    def test_lists_all_with_configured_flags(self, client, auth):
        body = client.get("/v1/models", headers=auth).json()
        by_id = {p["id"]: p for p in body}
        assert by_id["anthropic"]["configured"] is True
        assert by_id["groq"]["configured"] is False
        assert by_id["openrouter"]["default_model"]


class TestSubmit:
    def test_returns_202_with_tracking_urls(self, client, auth, stub_engine):
        response = client.post(
            "/v1/research", json={"query": "What is MCP?"}, headers=auth
        )
        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "queued"
        assert body["poll_url"].endswith(body["id"])
        assert body["events_url"].endswith("/events")
        assert "Location" in response.headers

    def test_requires_auth(self, client, stub_engine):
        assert client.post("/v1/research", json={"query": "hello there"}).status_code == 401

    def test_rejects_blank_query(self, client, auth, stub_engine):
        assert client.post("/v1/research", json={"query": "  "}, headers=auth).status_code == 422

    def test_rejects_unknown_option(self, client, auth, stub_engine):
        response = client.post(
            "/v1/research",
            json={"query": "valid query", "options": {"nonsense": 1}},
            headers=auth,
        )
        assert response.status_code == 422

    def test_rejects_unknown_provider(self, client, auth, stub_engine):
        response = client.post(
            "/v1/research",
            json={"query": "valid query", "options": {"provider": "nope"}},
            headers=auth,
        )
        assert response.status_code == 400

    def test_job_completes_and_carries_result(self, client, auth, stub_engine):
        job_id = client.post(
            "/v1/research", json={"query": "What is MCP?"}, headers=auth
        ).json()["id"]
        job = _wait_for(client, job_id, auth)
        assert job["result"]["report_markdown"].startswith("# Findings")
        assert job["result"]["sources"][0]["url"] == "https://example.com"
        assert job["duration_seconds"] is not None

    def test_metadata_is_echoed_back(self, client, auth, stub_engine):
        job_id = client.post(
            "/v1/research",
            json={"query": "question here", "metadata": {"tenant": "acme"}},
            headers=auth,
        ).json()["id"]
        job = _wait_for(client, job_id, auth)
        assert job["metadata"] == {"tenant": "acme"}

    def test_idempotency_key_returns_same_job(self, client, auth, stub_engine):
        payload = {"query": "same question", "idempotency_key": "abc-123"}
        first = client.post("/v1/research", json=payload, headers=auth).json()["id"]
        second = client.post("/v1/research", json=payload, headers=auth).json()["id"]
        assert first == second
        assert len(stub_engine.calls) == 1

    def test_callback_url_rejected_without_secret(self, client, auth, stub_engine, settings):
        settings.webhook_secret = ""
        response = client.post(
            "/v1/research",
            json={"query": "a question", "callback_url": "https://example.com/hook"},
            headers=auth,
        )
        assert response.status_code == 400
        assert "WEBHOOK_SECRET" in response.json()["detail"]


class TestFailure:
    def test_engine_error_becomes_failed_job(self, client, auth, stub_engine):
        stub_engine.fail_with = RuntimeError("model exploded")
        job_id = client.post(
            "/v1/research", json={"query": "a question"}, headers=auth
        ).json()["id"]
        job = _wait_for(client, job_id, auth, target="failed")
        assert job["error"]["code"] == "research_failed"
        assert job["error"]["retryable"] is False

    def test_rate_limit_errors_are_marked_retryable(self, client, auth, stub_engine):
        stub_engine.fail_with = RuntimeError("429 rate limit exceeded")
        job_id = client.post(
            "/v1/research", json={"query": "a question"}, headers=auth
        ).json()["id"]
        job = _wait_for(client, job_id, auth, target="failed")
        assert job["error"]["retryable"] is True


class TestRetrieval:
    def test_unknown_job_404s(self, client, auth):
        assert client.get("/v1/research/job_missing", headers=auth).status_code == 404

    def test_list_jobs(self, client, auth, stub_engine):
        for i in range(3):
            client.post("/v1/research", json={"query": f"question {i}"}, headers=auth)
        assert len(client.get("/v1/research", headers=auth).json()) >= 3

    def test_events_stream_replays_backlog(self, client, auth, stub_engine):
        job_id = client.post(
            "/v1/research", json={"query": "a question"}, headers=auth
        ).json()["id"]
        _wait_for(client, job_id, auth)
        # subscribing after completion still yields the whole run
        with client.stream(
            "GET", f"/v1/research/{job_id}/events", headers=auth
        ) as response:
            assert response.status_code == 200
            body = "".join(response.iter_text())
        assert "stage.start" in body
        assert "job.succeeded" in body


class TestRateLimit:
    def test_429_after_limit(self, client, auth, stub_engine):
        codes = [
            client.post("/v1/research", json={"query": f"q number {i}"}, headers=auth).status_code
            for i in range(8)
        ]
        assert 429 in codes
        assert codes.count(202) <= 5
