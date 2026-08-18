"""Event fan-out, with a backlog so late subscribers see the whole run.

The Redis implementation is what lets a client poll replica B and still watch a
job executing on replica A. Two things it has to get right:

* **No gap.** Subscribe to the channel *before* reading the backlog. Doing it the
  other way round drops any event published in between.
* **No duplicate.** Because of the above, the tail of the backlog and the head of
  the live channel overlap. Events carry a monotonic `sequence`, so the overlap
  is discarded by sequence rather than by guesswork.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator
from typing import Protocol

from .config import Settings
from .schemas import Event

logger = logging.getLogger(__name__)

_BACKLOG = "dr:ev:{job_id}"
_CHANNEL = "dr:ev:ch:{job_id}"
_CLOSED = "dr:ev:closed:{job_id}"
_CONTROL = "dr:control"

# Sentinel published when a job ends, so subscribers stop rather than hang.
_END = "__end__"


class EventBus(Protocol):
    async def publish(self, job_id: str, event: Event) -> None: ...
    async def close_stream(self, job_id: str) -> None: ...
    def subscribe(self, job_id: str) -> AsyncGenerator[Event, None]: ...
    async def broadcast_cancel(self, job_id: str) -> None: ...
    async def close(self) -> None: ...


class InMemoryEventBus:
    """Per-process fan-out."""

    class _Channel:
        def __init__(self) -> None:
            self.backlog: list[Event] = []
            self.subscribers: set[asyncio.Queue[Event | None]] = set()
            self.closed = False

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._channels: dict[str, InMemoryEventBus._Channel] = {}

    def _channel(self, job_id: str) -> InMemoryEventBus._Channel:
        return self._channels.setdefault(job_id, self._Channel())

    async def publish(self, job_id: str, event: Event) -> None:
        channel = self._channel(job_id)
        channel.backlog.append(event)
        for q in list(channel.subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(event)

    async def close_stream(self, job_id: str) -> None:
        channel = self._channel(job_id)
        channel.closed = True
        for q in list(channel.subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(None)

    async def subscribe(self, job_id: str) -> AsyncGenerator[Event, None]:
        channel = self._channel(job_id)
        q: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=1000)
        for event in channel.backlog:
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(event)
        if channel.closed:
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(None)
        channel.subscribers.add(q)

        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15.0)
                except TimeoutError:
                    yield Event(type="heartbeat", job_id=job_id)  # type: ignore[arg-type]
                    continue
                if event is None:
                    return
                yield event
        finally:
            channel.subscribers.discard(q)

    async def broadcast_cancel(self, job_id: str) -> None:
        return None  # the owning process is this process

    async def close(self) -> None:
        return None


class RedisEventBus:
    """Cross-replica fan-out over Redis pub/sub with a durable backlog."""

    def __init__(self, settings: Settings, redis) -> None:  # noqa: ANN001
        self.settings = settings
        self.redis = redis
        self.ttl = settings.job_retention_seconds
        self._cancel_hooks: dict[str, asyncio.Event] = {}
        self._control_task: asyncio.Task | None = None

    @classmethod
    async def create(cls, settings: Settings) -> RedisEventBus:
        from redis.asyncio import Redis

        redis = Redis.from_url(
            settings.redis_url, encoding="utf-8", decode_responses=True
        )
        await redis.ping()
        bus = cls(settings, redis)
        bus._control_task = asyncio.create_task(bus._listen_for_control())
        return bus

    async def publish(self, job_id: str, event: Event) -> None:
        payload = event.model_dump_json()
        pipe = self.redis.pipeline()
        pipe.rpush(_BACKLOG.format(job_id=job_id), payload)
        pipe.expire(_BACKLOG.format(job_id=job_id), self.ttl)
        pipe.publish(_CHANNEL.format(job_id=job_id), payload)
        await pipe.execute()

    async def close_stream(self, job_id: str) -> None:
        pipe = self.redis.pipeline()
        pipe.set(_CLOSED.format(job_id=job_id), "1", ex=self.ttl)
        pipe.publish(_CHANNEL.format(job_id=job_id), _END)
        await pipe.execute()

    async def subscribe(self, job_id: str) -> AsyncGenerator[Event, None]:
        pubsub = self.redis.pubsub()
        # Subscribe FIRST, then read the backlog -- see the module docstring.
        await pubsub.subscribe(_CHANNEL.format(job_id=job_id))

        try:
            raw_backlog = await self.redis.lrange(_BACKLOG.format(job_id=job_id), 0, -1)
            highest = -1
            for raw in raw_backlog:
                event = Event.model_validate_json(raw)
                highest = max(highest, event.sequence)
                yield event

            # If the job finished while we were reading the backlog, the sentinel
            # may already have gone out on the channel and been missed.
            if await self.redis.exists(_CLOSED.format(job_id=job_id)):
                return

            while True:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=15.0
                )
                if message is None:
                    yield Event(type="heartbeat", job_id=job_id)  # type: ignore[arg-type]
                    continue

                data = message["data"]
                if data == _END:
                    return

                event = Event.model_validate_json(data)
                # Discard the overlap between backlog tail and live channel head.
                if event.sequence <= highest:
                    continue
                highest = event.sequence
                yield event
        finally:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe(_CHANNEL.format(job_id=job_id))
                await pubsub.aclose()

    # -- cross-replica cancellation -------------------------------------

    def register_cancel_hook(self, job_id: str, event: asyncio.Event) -> None:
        """Called by the replica that owns the running job."""
        self._cancel_hooks[job_id] = event

    def unregister_cancel_hook(self, job_id: str) -> None:
        self._cancel_hooks.pop(job_id, None)

    async def broadcast_cancel(self, job_id: str) -> None:
        """A cancel can land on any replica; the owner may be a different one."""
        await self.redis.publish(_CONTROL, f"cancel:{job_id}")

    async def _listen_for_control(self) -> None:
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(_CONTROL)
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                action, _, job_id = str(message["data"]).partition(":")
                if action == "cancel" and (hook := self._cancel_hooks.get(job_id)):
                    logger.info("cancelling job %s on this replica", job_id)
                    hook.set()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("control channel listener stopped")
        finally:
            with contextlib.suppress(Exception):
                await pubsub.aclose()

    async def close(self) -> None:
        if self._control_task:
            self._control_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._control_task
        await self.redis.aclose()


async def build_event_bus(settings: Settings) -> EventBus:
    if settings.job_backend == "redis":
        return await RedisEventBus.create(settings)
    return InMemoryEventBus(settings)
