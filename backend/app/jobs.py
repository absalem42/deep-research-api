"""Job engine: submit, run, fan out events, deliver webhooks.

This is what makes the service integrable. A research run takes 30-120s, so the
API hands back a job id immediately and the caller polls, streams, or waits for a
signed webhook.

State lives behind `JobStore` and `EventBus` (see `store.py` / `eventbus.py`), so
the same orchestration runs single-process or across replicas depending on
`JOB_BACKEND`.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncGenerator

import httpx

from .config import Settings
from .eventbus import EventBus, RedisEventBus, build_event_bus
from .research import DeepResearchEngine, ResearchCancelled, ResearchTimeout
from .schemas import (
    Event,
    EventType,
    Job,
    JobStatus,
    ResearchError,
    ResearchOptions,
    ResearchRequest,
    ResearchStage,
    WebhookPayload,
    utcnow,
)
from .security import sign_payload
from .store import JobStore, build_store

logger = logging.getLogger(__name__)


class JobNotFound(KeyError):
    pass


class JobManager:
    def __init__(
        self,
        settings: Settings,
        engine: DeepResearchEngine,
        store: JobStore,
        bus: EventBus,
    ) -> None:
        self.settings = settings
        self.engine = engine
        self.store = store
        self.bus = bus
        self._tasks: dict[str, asyncio.Task] = {}
        self._cancels: dict[str, asyncio.Event] = {}
        self._sem = asyncio.Semaphore(settings.max_concurrent_jobs)
        self._lock = asyncio.Lock()

    @classmethod
    async def create(
        cls, settings: Settings, engine: DeepResearchEngine
    ) -> JobManager:
        return cls(
            settings, engine, await build_store(settings), await build_event_bus(settings)
        )

    # -- lifecycle ------------------------------------------------------

    async def submit(self, request: ResearchRequest) -> Job:
        spec, model, _ = self.engine.resolve(request.options)
        now = utcnow()
        job = Job(
            id=f"job_{uuid.uuid4().hex[:24]}",
            status=JobStatus.QUEUED,
            query=request.query,
            provider=spec.id,
            model=model,
            created_at=now,
            updated_at=now,
            metadata=request.metadata,
            callback_url=str(request.callback_url) if request.callback_url else None,
        )

        async with self._lock:
            if request.idempotency_key:
                winner = await self.store.claim_idempotency_key(
                    request.idempotency_key, job.id
                )
                if winner != job.id and (existing := await self.store.get(winner)):
                    return existing
            await self.store.save(job)

        self._cancels[job.id] = asyncio.Event()
        if isinstance(self.bus, RedisEventBus):
            self.bus.register_cancel_hook(job.id, self._cancels[job.id])

        self._tasks[job.id] = asyncio.create_task(self._run(job.id, request.options))
        return job

    async def get(self, job_id: str) -> Job:
        job = await self.store.get(job_id)
        if job is None:
            raise JobNotFound(job_id)
        return job

    async def list(self, limit: int = 50, status: JobStatus | None = None) -> list[Job]:
        return await self.store.list(limit, status)

    async def cancel(self, job_id: str) -> Job:
        job = await self.get(job_id)
        if job.status.terminal:
            return job
        await self.store.request_cancel(job_id)
        if event := self._cancels.get(job_id):
            event.set()  # owned here
        else:
            await self.bus.broadcast_cancel(job_id)  # owned by another replica
        return job

    async def _run(self, job_id: str, options: ResearchOptions) -> None:
        async with self._sem:
            job = await self.get(job_id)
            cancel = self._cancels[job_id]

            job.status = JobStatus.RUNNING
            job.started_at = utcnow()
            job.updated_at = job.started_at
            await self.store.save(job)
            started = time.monotonic()

            try:
                async for event, result in self.engine.stream(
                    job.query, options, job_id, cancel
                ):
                    await self.bus.publish(job_id, event)
                    if result is not None:
                        job.result = result
                job.status = JobStatus.SUCCEEDED

            except ResearchCancelled:
                job.status = JobStatus.CANCELLED
                job.error = ResearchError(
                    code="cancelled", message="Cancelled by request.", retryable=False
                )
            except ResearchTimeout as exc:
                job.status = JobStatus.FAILED
                job.error = ResearchError(code="timeout", message=str(exc), retryable=True)
            except Exception as exc:  # noqa: BLE001
                logger.exception("job %s failed", job_id)
                job.status = JobStatus.FAILED
                job.error = ResearchError(
                    code="research_failed",
                    message=str(exc)[:2000],
                    retryable=_looks_transient(exc),
                )
            finally:
                job.finished_at = utcnow()
                job.updated_at = job.finished_at
                job.duration_seconds = round(time.monotonic() - started, 2)
                await self.store.save(job)

                if job.status is not JobStatus.SUCCEEDED:
                    await self.bus.publish(
                        job_id,
                        Event(
                            type=EventType.JOB_FAILED,
                            job_id=job_id,
                            stage=ResearchStage.COMPLETE,
                            content=job.error.message if job.error else None,
                            data={"status": job.status.value},
                        ),
                    )
                await self.bus.close_stream(job_id)

                self._tasks.pop(job_id, None)
                self._cancels.pop(job_id, None)
                if isinstance(self.bus, RedisEventBus):
                    self.bus.unregister_cancel_hook(job_id)

            if job.callback_url:
                asyncio.create_task(self._deliver(job))

    # -- events ---------------------------------------------------------

    async def events(self, job_id: str) -> AsyncGenerator[Event, None]:
        if await self.store.get(job_id) is None:
            raise JobNotFound(job_id)
        async for event in self.bus.subscribe(job_id):
            yield event

    # -- webhooks -------------------------------------------------------

    async def _deliver(self, job: Job) -> None:
        """POST the finished job to the caller's callback URL.

        Signed HMAC-SHA256 over "<timestamp>.<body>", retried with exponential
        backoff. Retries only on 5xx/429/network -- a 4xx means the receiver
        rejected the payload and replaying will not help.
        """
        s = self.settings
        if not s.webhook_secret:
            logger.warning(
                "job %s has a callback_url but WEBHOOK_SECRET is unset; "
                "refusing to send an unsigned webhook",
                job.id,
            )
            return

        payload = WebhookPayload(
            event=(
                "research.succeeded"
                if job.status is JobStatus.SUCCEEDED
                else "research.failed"
            ),
            job=job,
        )
        body = payload.model_dump_json().encode()
        delay = 1.0

        async with httpx.AsyncClient(timeout=s.webhook_timeout_seconds) as client:
            for attempt in range(1, s.webhook_max_attempts + 1):
                signature, _ = sign_payload(s.webhook_secret, body)
                try:
                    response = await client.post(
                        job.callback_url,  # type: ignore[arg-type]
                        content=body,
                        headers={
                            "Content-Type": "application/json",
                            "X-DeepResearch-Signature": signature,
                            "X-DeepResearch-Event": payload.event,
                            "X-DeepResearch-Job-Id": job.id,
                            "X-DeepResearch-Attempt": str(attempt),
                            "User-Agent": f"{s.service_name}-webhook/1.0",
                        },
                    )
                    if response.status_code < 300:
                        logger.info("webhook delivered job=%s attempt=%d", job.id, attempt)
                        return
                    if response.status_code < 500 and response.status_code != 429:
                        logger.error(
                            "webhook rejected job=%s status=%d; not retrying",
                            job.id,
                            response.status_code,
                        )
                        return
                    logger.warning(
                        "webhook attempt %d for job=%s got %d",
                        attempt,
                        job.id,
                        response.status_code,
                    )
                except httpx.HTTPError as exc:
                    logger.warning(
                        "webhook attempt %d for job=%s failed: %s", attempt, job.id, exc
                    )

                if attempt < s.webhook_max_attempts:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 60.0)

        logger.error("webhook permanently failed job=%s", job.id)

    # -- shutdown -------------------------------------------------------

    async def shutdown(self) -> None:
        for event in self._cancels.values():
            event.set()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        await self.bus.close()
        await self.store.close()


def _looks_transient(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(
        marker in text
        for marker in (
            "rate limit", "429", "timeout", "timed out", "connection",
            "temporarily", "overloaded", "503", "502", "504",
        )
    )
