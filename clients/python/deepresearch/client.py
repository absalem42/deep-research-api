"""Python client for the Deep Research API.

    from deepresearch import DeepResearchClient

    client = DeepResearchClient("https://research.example.com", api_key="drk_...")
    report = client.research("What changed in EU AI Act enforcement in 2026?")
    print(report.report_markdown)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

import httpx

__all__ = [
    "DeepResearchClient",
    "AsyncDeepResearchClient",
    "Job",
    "DeepResearchError",
    "ResearchFailed",
    "ResearchTimeout",
    "verify_webhook",
]


class DeepResearchError(RuntimeError):
    """Transport or API-level failure."""


class ResearchFailed(DeepResearchError):
    """The job ran and ended in a failed state."""

    def __init__(self, message: str, *, code: str = "", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ResearchTimeout(DeepResearchError):
    """Local wait budget elapsed. The job may still be running server-side."""

    def __init__(self, message: str, job_id: str):
        super().__init__(message)
        self.job_id = job_id


@dataclass
class Job:
    id: str
    status: str
    query: str = ""
    provider: str = ""
    model: str = ""
    duration_seconds: float | None = None
    report_markdown: str = ""
    sources: list[dict[str, Any]] = field(default_factory=list)
    error: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Job":
        result = payload.get("result") or {}
        return cls(
            id=payload["id"],
            status=payload["status"],
            query=payload.get("query", ""),
            provider=payload.get("provider", ""),
            model=payload.get("model", ""),
            duration_seconds=payload.get("duration_seconds"),
            report_markdown=result.get("report_markdown", ""),
            sources=result.get("sources", []),
            error=payload.get("error"),
            raw=payload,
        )

    @property
    def done(self) -> bool:
        return self.status in ("succeeded", "failed", "cancelled")


def _options(**kwargs: Any) -> dict[str, Any]:
    return {k: v for k, v in kwargs.items() if v is not None}


class DeepResearchClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    # -- low level ------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._client.request(method, f"{self.base_url}{path}", **kwargs)
        except httpx.HTTPError as exc:
            raise DeepResearchError(f"Request to {path} failed: {exc}") from exc
        if response.status_code >= 400:
            detail = ""
            try:
                detail = response.json().get("detail", "")
            except Exception:  # noqa: BLE001
                detail = response.text[:400]
            raise DeepResearchError(f"{response.status_code} from {path}: {detail}")
        return response.json()

    # -- api ------------------------------------------------------------

    def providers(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/models")

    def start(
        self,
        query: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        search_api: str | None = None,
        max_concurrent_research_units: int | None = None,
        callback_url: str | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """Submit a job and return its id without waiting."""
        payload: dict[str, Any] = {
            "query": query,
            "options": _options(
                provider=provider,
                model=model,
                search_api=search_api,
                max_concurrent_research_units=max_concurrent_research_units,
            ),
        }
        if callback_url:
            payload["callback_url"] = callback_url
        if metadata:
            payload["metadata"] = metadata
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        return self._request("POST", "/v1/research", json=payload)["id"]

    def get(self, job_id: str) -> Job:
        return Job.from_payload(self._request("GET", f"/v1/research/{job_id}"))

    def cancel(self, job_id: str) -> Job:
        return Job.from_payload(self._request("DELETE", f"/v1/research/{job_id}"))

    def wait(
        self,
        job_id: str,
        *,
        poll_interval: float = 3.0,
        max_wait: float = 900.0,
        on_update: Callable[[Job], None] | None = None,
    ) -> Job:
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            job = self.get(job_id)
            if on_update:
                on_update(job)
            if job.done:
                if job.status == "failed":
                    error = job.error or {}
                    raise ResearchFailed(
                        error.get("message", "research failed"),
                        code=error.get("code", ""),
                        retryable=bool(error.get("retryable")),
                    )
                return job
            time.sleep(poll_interval)
        raise ResearchTimeout(
            f"job {job_id} did not finish within {max_wait}s", job_id
        )

    def research(self, query: str, *, max_wait: float = 900.0, **kwargs: Any) -> Job:
        """Submit and block until the report is ready."""
        return self.wait(self.start(query, **kwargs), max_wait=max_wait)

    def stream(self, job_id: str) -> Iterator[dict[str, Any]]:
        """Yield SSE events as dicts until the job finishes."""
        url = f"{self.base_url}/v1/research/{job_id}/events"
        with self._client.stream("GET", url, timeout=None) as response:
            if response.status_code >= 400:
                raise DeepResearchError(f"{response.status_code} opening event stream")
            for line in response.iter_lines():
                if line.startswith("data: "):
                    yield json.loads(line[6:])

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "DeepResearchClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class AsyncDeepResearchClient:
    """Async twin of DeepResearchClient."""

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self._client.request(
                method, f"{self.base_url}{path}", **kwargs
            )
        except httpx.HTTPError as exc:
            raise DeepResearchError(f"Request to {path} failed: {exc}") from exc
        if response.status_code >= 400:
            raise DeepResearchError(f"{response.status_code} from {path}: {response.text[:400]}")
        return response.json()

    async def start(self, query: str, **kwargs: Any) -> str:
        payload = {"query": query, "options": _options(**kwargs)}
        return (await self._request("POST", "/v1/research", json=payload))["id"]

    async def get(self, job_id: str) -> Job:
        return Job.from_payload(await self._request("GET", f"/v1/research/{job_id}"))

    async def research(
        self, query: str, *, poll_interval: float = 3.0, max_wait: float = 900.0, **kwargs: Any
    ) -> Job:
        import asyncio

        job_id = await self.start(query, **kwargs)
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            job = await self.get(job_id)
            if job.done:
                if job.status == "failed":
                    error = job.error or {}
                    raise ResearchFailed(
                        error.get("message", "research failed"),
                        code=error.get("code", ""),
                        retryable=bool(error.get("retryable")),
                    )
                return job
            await asyncio.sleep(poll_interval)
        raise ResearchTimeout(f"job {job_id} timed out after {max_wait}s", job_id)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "AsyncDeepResearchClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()


def verify_webhook(
    secret: str, body: bytes, signature_header: str, tolerance_seconds: int = 300
) -> bool:
    """Validate an incoming webhook before trusting it.

    Always call this on the receiving side -- the callback URL is public, so
    anyone can POST to it.
    """
    try:
        parts = dict(p.split("=", 1) for p in signature_header.split(",") if "=" in p)
        timestamp = int(parts["t"])
    except (ValueError, KeyError):
        return False
    if abs(time.time() - timestamp) > tolerance_seconds:
        return False
    expected = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(f"t={timestamp},v1={expected}", signature_header)
