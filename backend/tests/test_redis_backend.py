"""Redis job store and event bus, exercised against fakeredis.

The point of this backend is multi-replica operation, so most of these tests
build *two* independent components over one shared Redis and assert that what
one writes, the other sees.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from app.eventbus import InMemoryEventBus, RedisEventBus
from app.schemas import Event, EventType, Job, JobStatus, utcnow
from app.store import InMemoryJobStore, RedisJobStore

fakeredis = pytest.importorskip("fakeredis")


REDIS_URL = os.getenv("TEST_REDIS_URL")


@pytest.fixture
async def redis():
    """A real Redis when TEST_REDIS_URL is set, otherwise fakeredis.

    fakeredis is a good stand-in but it is still a reimplementation: pipelines,
    pub/sub delivery timing and TTL semantics are exactly the areas where it can
    diverge from the real server, and those are exactly what this backend leans
    on. CI runs this suite both ways -- see the `redis-integration` job.
    """
    if REDIS_URL:
        from redis.asyncio import Redis

        client = Redis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
        await client.ping()
        # Each test gets a clean keyspace; these tests assert on exact key state.
        await client.flushdb()
        try:
            yield client
        finally:
            await client.flushdb()
            await client.aclose()
    else:
        from fakeredis import aioredis

        yield aioredis.FakeRedis(decode_responses=True)


def test_which_redis_is_under_test():
    """Prints the backend in use so a green run is not ambiguous."""
    print(f"\nredis backend: {'REAL ' + REDIS_URL if REDIS_URL else 'fakeredis'}")


@pytest.fixture
def store(settings, redis) -> RedisJobStore:
    settings.job_backend = "redis"
    return RedisJobStore(settings, redis)


@pytest.fixture
def bus(settings, redis) -> RedisEventBus:
    settings.job_backend = "redis"
    return RedisEventBus(settings, redis)


def make_job(job_id: str = "job_1", status: JobStatus = JobStatus.QUEUED) -> Job:
    now = utcnow()
    return Job(
        id=job_id,
        status=status,
        query="what is deep research?",
        provider="anthropic",
        model="claude-sonnet-4",
        created_at=now,
        updated_at=now,
    )


class TestRedisJobStore:
    async def test_save_and_get_roundtrip(self, store):
        job = make_job()
        await store.save(job)

        loaded = await store.get("job_1")
        assert loaded is not None
        assert loaded.id == job.id
        assert loaded.query == job.query
        assert loaded.provider == "anthropic"

    async def test_missing_job_is_none(self, store):
        assert await store.get("job_nope") is None

    async def test_list_is_newest_first(self, store):
        for i in range(3):
            job = make_job(f"job_{i}")
            job.created_at = utcnow().replace(microsecond=i * 1000)
            await store.save(job)

        listed = await store.list(limit=10, status=None)
        assert [j.id for j in listed] == ["job_2", "job_1", "job_0"]

    async def test_list_honours_limit(self, store):
        for i in range(5):
            await store.save(make_job(f"job_{i}"))
        assert len(await store.list(limit=2, status=None)) == 2

    async def test_list_filters_by_status(self, store):
        await store.save(make_job("job_ok", JobStatus.SUCCEEDED))
        await store.save(make_job("job_bad", JobStatus.FAILED))

        succeeded = await store.list(limit=10, status=JobStatus.SUCCEEDED)
        assert [j.id for j in succeeded] == ["job_ok"]

    async def test_list_prunes_expired_index_entries(self, store, redis):
        """Redis does not drop sorted-set members when the keys they name expire."""
        await store.save(make_job("job_live"))
        await store.save(make_job("job_gone"))
        await redis.delete("dr:job:job_gone")  # simulate TTL expiry

        listed = await store.list(limit=10, status=None)
        assert [j.id for j in listed] == ["job_live"]
        assert await redis.zscore("dr:jobs", "job_gone") is None

    async def test_result_survives_the_roundtrip(self, store):
        from app.schemas import ResearchResult, Source

        job = make_job(status=JobStatus.SUCCEEDED)
        job.result = ResearchResult(
            report_markdown="# Report", sources=[Source(url="https://a.com", title="A")]
        )
        await store.save(job)

        loaded = await store.get("job_1")
        assert loaded.result.report_markdown == "# Report"
        assert loaded.result.sources[0].url == "https://a.com"


class TestIdempotencyAcrossReplicas:
    async def test_second_claim_returns_the_first_job(self, store):
        await store.save(make_job("job_first"))
        assert await store.claim_idempotency_key("key-1", "job_first") == "job_first"
        assert await store.claim_idempotency_key("key-1", "job_second") == "job_first"

    async def test_concurrent_claims_pick_exactly_one_winner(self, settings, redis):
        """Two replicas receiving the same retry at once must agree."""
        settings.job_backend = "redis"
        replica_a = RedisJobStore(settings, redis)
        replica_b = RedisJobStore(settings, redis)

        await replica_a.save(make_job("job_a"))
        await replica_b.save(make_job("job_b"))

        winners = await asyncio.gather(
            replica_a.claim_idempotency_key("shared", "job_a"),
            replica_b.claim_idempotency_key("shared", "job_b"),
        )
        assert winners[0] == winners[1]
        assert winners[0] in {"job_a", "job_b"}

    async def test_key_is_reusable_once_the_job_expired(self, store, redis):
        await store.save(make_job("job_old"))
        await store.claim_idempotency_key("key-2", "job_old")
        await redis.delete("dr:job:job_old")

        assert await store.claim_idempotency_key("key-2", "job_new") == "job_new"


class TestRedisEventBus:
    async def _drain(self, bus, job_id, expected):
        """Collect `expected` events, then stop. Fails loudly rather than hanging."""
        collected = []
        async for event in bus.subscribe(job_id):
            if event.type == EventType.HEARTBEAT:
                continue
            collected.append(event)
            if len(collected) >= expected:
                break
        return collected

    async def test_late_subscriber_replays_the_backlog(self, bus):
        for i in range(3):
            await bus.publish(
                "job_1",
                Event(type=EventType.THINKING, job_id="job_1", sequence=i, content=f"e{i}"),
            )
        await bus.close_stream("job_1")

        collected = [e async for e in bus.subscribe("job_1") if e.type != EventType.HEARTBEAT]
        assert [e.content for e in collected] == ["e0", "e1", "e2"]

    async def test_closed_stream_terminates_the_subscriber(self, bus):
        await bus.publish(
            "job_1", Event(type=EventType.THINKING, job_id="job_1", sequence=1)
        )
        await bus.close_stream("job_1")

        # completes rather than hanging on the live channel
        events = await asyncio.wait_for(
            self._collect_until_done(bus, "job_1"), timeout=5.0
        )
        assert len(events) == 1

    async def _collect_until_done(self, bus, job_id):
        return [e async for e in bus.subscribe(job_id) if e.type != EventType.HEARTBEAT]

    async def test_live_events_reach_a_second_replica(self, settings, redis):
        """The whole reason this backend exists."""
        settings.job_backend = "redis"
        writer = RedisEventBus(settings, redis)
        reader = RedisEventBus(settings, redis)

        received: list[Event] = []

        async def consume():
            async for event in reader.subscribe("job_x"):
                if event.type == EventType.HEARTBEAT:
                    continue
                received.append(event)
                if len(received) >= 2:
                    break

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.1)  # let the subscription establish

        await writer.publish(
            "job_x", Event(type=EventType.THINKING, job_id="job_x", sequence=1, content="a")
        )
        await writer.publish(
            "job_x", Event(type=EventType.SOURCES, job_id="job_x", sequence=2, content="b")
        )

        await asyncio.wait_for(task, timeout=5.0)
        assert [e.content for e in received] == ["a", "b"]

    async def test_backlog_and_live_overlap_is_deduplicated(self, settings, redis):
        """Subscribing mid-run must not double-deliver the overlap.

        The subscriber joins the channel before reading the backlog, so an event
        can arrive by both routes. Sequence numbers resolve it.
        """
        settings.job_backend = "redis"
        bus = RedisEventBus(settings, redis)

        await bus.publish(
            "job_y", Event(type=EventType.THINKING, job_id="job_y", sequence=1, content="old")
        )

        received: list[Event] = []

        async def consume():
            async for event in bus.subscribe("job_y"):
                if event.type == EventType.HEARTBEAT:
                    continue
                received.append(event)
                if len(received) >= 2:
                    break

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.1)

        # replay sequence 1 on the live channel, then a genuinely new event
        await bus.redis.publish(
            "dr:ev:ch:job_y",
            Event(
                type=EventType.THINKING, job_id="job_y", sequence=1, content="old"
            ).model_dump_json(),
        )
        await bus.publish(
            "job_y", Event(type=EventType.SOURCES, job_id="job_y", sequence=2, content="new")
        )

        await asyncio.wait_for(task, timeout=5.0)
        assert [e.content for e in received] == ["old", "new"]

    async def test_cancel_broadcast_reaches_the_owning_replica(self, settings, redis):
        settings.job_backend = "redis"
        owner = RedisEventBus(settings, redis)
        owner._control_task = asyncio.create_task(owner._listen_for_control())
        other = RedisEventBus(settings, redis)

        cancel_event = asyncio.Event()
        owner.register_cancel_hook("job_z", cancel_event)
        await asyncio.sleep(0.1)

        # the cancel lands on a replica that is not running the job
        await other.broadcast_cancel("job_z")

        await asyncio.wait_for(cancel_event.wait(), timeout=5.0)
        assert cancel_event.is_set()

        await owner.close()


class TestMultiReplica:
    """End-to-end proof of the claim: two JobManagers, one shared Redis."""

    @pytest.fixture
    def engine(self, settings):
        from app.providers import get_provider
        from app.schemas import ResearchResult, Source

        class StubEngine:
            def resolve(self, options):
                spec = get_provider(options.provider or "anthropic")
                return spec, spec.default_model, "tavily"

            async def stream(self, query, options, job_id, cancel_event=None, prior_context=None):
                yield Event(
                    type=EventType.STAGE_START,
                    job_id=job_id,
                    sequence=1,
                    content="brief",
                ), None
                await asyncio.sleep(0.05)
                yield Event(
                    type=EventType.JOB_SUCCEEDED, job_id=job_id, sequence=2
                ), ResearchResult(
                    report_markdown="# Cross-replica report",
                    sources=[Source(url="https://a.com")],
                )

        return StubEngine()

    @pytest.fixture
    def replicas(self, settings, redis, engine):
        from app.jobs import JobManager

        settings.job_backend = "redis"
        a = JobManager(settings, engine, RedisJobStore(settings, redis), RedisEventBus(settings, redis))
        b = JobManager(settings, engine, RedisJobStore(settings, redis), RedisEventBus(settings, redis))
        return a, b

    async def test_job_submitted_on_a_is_readable_on_b(self, replicas):
        from app.schemas import ResearchRequest

        replica_a, replica_b = replicas
        job = await replica_a.submit(ResearchRequest(query="a shared question"))

        # B never saw the submission, but shares the store
        from_b = await replica_b.get(job.id)
        assert from_b.id == job.id
        assert from_b.query == "a shared question"

    async def test_result_written_by_a_is_visible_on_b(self, replicas):
        from app.schemas import ResearchRequest

        replica_a, replica_b = replicas
        job = await replica_a.submit(ResearchRequest(query="a shared question"))

        for _ in range(100):
            current = await replica_b.get(job.id)
            if current.status.terminal:
                break
            await asyncio.sleep(0.05)

        assert current.status == JobStatus.SUCCEEDED
        assert current.result.report_markdown == "# Cross-replica report"

    async def test_b_can_stream_events_for_a_job_running_on_a(self, replicas):
        from app.schemas import ResearchRequest

        replica_a, replica_b = replicas
        job = await replica_a.submit(ResearchRequest(query="a shared question"))

        collected = []
        async for event in replica_b.events(job.id):
            if event.type == EventType.HEARTBEAT:
                continue
            collected.append(event.type)

        assert EventType.STAGE_START in collected
        assert EventType.JOB_SUCCEEDED in collected

    async def test_idempotent_retry_across_replicas_runs_once(self, replicas):
        from app.schemas import ResearchRequest

        replica_a, replica_b = replicas
        request = ResearchRequest(query="a shared question", idempotency_key="retry-1")

        first = await replica_a.submit(request)
        second = await replica_b.submit(request)

        assert first.id == second.id


class TestBackendParity:
    """Both backends must behave the same through the shared interface."""

    @pytest.fixture(params=["memory", "redis"])
    def any_store(self, request, settings, redis):
        if request.param == "memory":
            return InMemoryJobStore(settings)
        return RedisJobStore(settings, redis)

    async def test_save_get_roundtrip(self, any_store):
        await any_store.save(make_job())
        assert (await any_store.get("job_1")).query == "what is deep research?"

    async def test_idempotency_returns_the_original(self, any_store):
        await any_store.save(make_job("job_first"))
        await any_store.claim_idempotency_key("k", "job_first")
        assert await any_store.claim_idempotency_key("k", "job_other") == "job_first"

    async def test_cancel_flag(self, any_store):
        assert not await any_store.is_cancelled("job_1")
        await any_store.request_cancel("job_1")
        assert await any_store.is_cancelled("job_1")

    @pytest.fixture(params=["memory", "redis"])
    def any_bus(self, request, settings, redis):
        if request.param == "memory":
            return InMemoryEventBus(settings)
        return RedisEventBus(settings, redis)

    async def test_backlog_replay_after_close(self, any_bus):
        await any_bus.publish(
            "job_1", Event(type=EventType.THINKING, job_id="job_1", sequence=1, content="x")
        )
        await any_bus.close_stream("job_1")

        collected = [
            e
            async for e in any_bus.subscribe("job_1")
            if e.type != EventType.HEARTBEAT
        ]
        assert [e.content for e in collected] == ["x"]
