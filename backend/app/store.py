"""Job persistence, behind one interface with two implementations.

`memory` keeps everything in the process -- fine for a single container, and the
default. `redis` puts job records in Redis so several replicas share one view of
the world, which is what makes horizontal scaling possible.

The interface is deliberately async everywhere, even where the in-memory
implementation has nothing to await. Making `get()` sync for memory and async for
Redis would push the backend choice into every call site.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Protocol

from .config import Settings
from .schemas import Job, JobStatus, utcnow

logger = logging.getLogger(__name__)

# Key layout. Namespaced so the service can share a Redis instance with others.
_JOB = "dr:job:{job_id}"
_INDEX = "dr:jobs"
_IDEMPOTENCY = "dr:idem:{key}"


class JobStore(Protocol):
    async def save(self, job: Job) -> None: ...
    async def get(self, job_id: str) -> Job | None: ...
    async def list(self, limit: int, status: JobStatus | None) -> list[Job]: ...
    async def claim_idempotency_key(self, key: str, job_id: str) -> str: ...
    async def request_cancel(self, job_id: str) -> None: ...
    async def is_cancelled(self, job_id: str) -> bool: ...
    async def close(self) -> None: ...


class InMemoryJobStore:
    """Single-process store. A restart loses everything -- by design, documented."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._idempotency: dict[str, str] = {}
        self._cancelled: set[str] = set()

    async def save(self, job: Job) -> None:
        self._jobs[job.id] = job
        self._evict()

    async def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    async def list(self, limit: int, status: JobStatus | None) -> list[Job]:
        jobs = list(reversed(self._jobs.values()))
        if status:
            jobs = [j for j in jobs if j.status == status]
        return jobs[:limit]

    async def claim_idempotency_key(self, key: str, job_id: str) -> str:
        """Return the winning job id -- the existing one if the key is taken."""
        existing = self._idempotency.get(key)
        if existing and existing in self._jobs:
            return existing
        self._idempotency[key] = job_id
        return job_id

    async def request_cancel(self, job_id: str) -> None:
        self._cancelled.add(job_id)

    async def is_cancelled(self, job_id: str) -> bool:
        return job_id in self._cancelled

    async def close(self) -> None:
        return None

    def _evict(self) -> None:
        cutoff = utcnow().timestamp() - self.settings.job_retention_seconds
        for job_id in [
            jid
            for jid, job in self._jobs.items()
            if job.status.terminal and job.updated_at.timestamp() < cutoff
        ]:
            self._jobs.pop(job_id, None)
            self._cancelled.discard(job_id)


class RedisJobStore:
    """Shared store so several replicas serve the same jobs.

    Every key carries a TTL of `job_retention_seconds`, so expiry is Redis's
    problem rather than a sweeper thread's. The index is a sorted set scored by
    creation time; expired ids are pruned from it lazily on read, because Redis
    does not remove members of a sorted set when the keys they name expire.
    """

    def __init__(self, settings: Settings, redis) -> None:  # noqa: ANN001
        self.settings = settings
        self.redis = redis
        self.ttl = settings.job_retention_seconds

    @classmethod
    async def create(cls, settings: Settings) -> RedisJobStore:
        try:
            from redis.asyncio import Redis
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise RuntimeError(
                "JOB_BACKEND=redis requires the 'redis' package. "
                "pip install -r requirements.txt"
            ) from exc

        redis = Redis.from_url(
            settings.redis_url, encoding="utf-8", decode_responses=True
        )
        # Fail at startup rather than on the first request.
        await redis.ping()
        logger.info("job store: redis at %s", settings.redis_url)
        return cls(settings, redis)

    async def save(self, job: Job) -> None:
        payload = job.model_dump_json()
        pipe = self.redis.pipeline()
        pipe.set(_JOB.format(job_id=job.id), payload, ex=self.ttl)
        pipe.zadd(_INDEX, {job.id: job.created_at.timestamp()})
        # Bound the index even if individual entries linger.
        pipe.zremrangebyscore(_INDEX, "-inf", utcnow().timestamp() - self.ttl)
        await pipe.execute()

    async def get(self, job_id: str) -> Job | None:
        raw = await self.redis.get(_JOB.format(job_id=job_id))
        if raw is None:
            return None
        return Job.model_validate_json(raw)

    async def list(self, limit: int, status: JobStatus | None) -> list[Job]:
        # Over-fetch: some ids in the index may have expired, and a status filter
        # discards more. Cap the scan so a huge index cannot stall a request.
        scan = min(max(limit * 4, limit), 1000)
        job_ids = await self.redis.zrevrange(_INDEX, 0, scan - 1)
        if not job_ids:
            return []

        raws = await self.redis.mget([_JOB.format(job_id=j) for j in job_ids])

        jobs: list[Job] = []
        stale: list[str] = []
        for job_id, raw in zip(job_ids, raws, strict=True):
            if raw is None:
                stale.append(job_id)
                continue
            job = Job.model_validate_json(raw)
            if status and job.status != status:
                continue
            jobs.append(job)
            if len(jobs) >= limit:
                break

        if stale:
            await self.redis.zrem(_INDEX, *stale)
        return jobs

    async def claim_idempotency_key(self, key: str, job_id: str) -> str:
        """Atomic claim via SET NX.

        Two replicas can receive the same retry simultaneously; NX means exactly
        one wins and the loser is told which job id to return.
        """
        redis_key = _IDEMPOTENCY.format(key=key)
        won = await self.redis.set(redis_key, job_id, nx=True, ex=self.ttl)
        if won:
            return job_id
        existing = await self.redis.get(redis_key)
        if existing and await self.redis.exists(_JOB.format(job_id=existing)):
            return existing
        # The claimed job expired; take the key over.
        await self.redis.set(redis_key, job_id, ex=self.ttl)
        return job_id

    async def request_cancel(self, job_id: str) -> None:
        await self.redis.set(f"dr:cancel:{job_id}", "1", ex=self.ttl)

    async def is_cancelled(self, job_id: str) -> bool:
        return bool(await self.redis.exists(f"dr:cancel:{job_id}"))

    async def close(self) -> None:
        await self.redis.aclose()


async def build_store(settings: Settings) -> JobStore:
    if settings.job_backend == "redis":
        return await RedisJobStore.create(settings)
    logger.info("job store: in-memory (single replica only)")
    return InMemoryJobStore(settings)
