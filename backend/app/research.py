"""Wraps the LangGraph deep-research agent and normalises its output.

The graph itself is vendored under `vendor/open_deep_research` (LangChain's
open_deep_research, MIT). Everything here is the production shell around it:
per-request configuration, event normalisation, timeouts, and cancellation.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from .config import Settings
from .providers import (
    ProviderSpec,
    api_key_for,
    get_provider,
    resolve_search_api,
)
from .schemas import (
    Event,
    EventType,
    ResearchOptions,
    ResearchResult,
    ResearchStage,
    Source,
    StageTiming,
)

logger = logging.getLogger(__name__)

_VENDOR_DIR = Path(__file__).resolve().parent.parent / "vendor"


def _ensure_vendor_on_path() -> None:
    """Make the vendored graph importable as a top-level package.

    Its modules import each other absolutely (`from open_deep_research.state
    import ...`), so `vendor/` itself has to be on sys.path. Doing it here rather
    than at module import keeps the path mutation out of test collection.
    """
    path = str(_VENDOR_DIR)
    if path not in sys.path:
        sys.path.insert(0, path)


# LangGraph node name -> our public stage vocabulary. Keeping this mapping in one
# place means a graph refactor upstream touches exactly one dict.
_NODE_STAGE: dict[str, ResearchStage] = {
    "clarify_with_user": ResearchStage.CLARIFICATION,
    "write_research_brief": ResearchStage.RESEARCH_BRIEF,
    "research_supervisor": ResearchStage.SUPERVISION,
    "supervisor": ResearchStage.SUPERVISION,
    "supervisor_tools": ResearchStage.SUPERVISION,
    "researcher": ResearchStage.RESEARCH_EXECUTION,
    "researcher_tools": ResearchStage.RESEARCH_EXECUTION,
    "research_agent": ResearchStage.RESEARCH_EXECUTION,
    "compress_research": ResearchStage.COMPRESSION,
    "final_report_generation": ResearchStage.FINAL_REPORT,
}

_URL_RE = re.compile(r"https?://[^\s\)\]\>\"']+")


class ResearchCancelled(Exception):
    pass


class ResearchTimeout(Exception):
    pass


class DeepResearchEngine:
    """One instance per process; safe to share across requests."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # -- configuration --------------------------------------------------

    def resolve(self, options: ResearchOptions) -> tuple[ProviderSpec, str, str]:
        spec = get_provider(options.provider or self.settings.default_provider)
        model = options.model or spec.default_model
        search_api = resolve_search_api(spec, self.settings, options.search_api)
        return spec, model, search_api

    def build_config(self, options: ResearchOptions, job_id: str) -> dict[str, Any]:
        """Everything the graph needs, passed explicitly.

        Note what is absent: any mutation of os.environ. The reference
        implementation set ANTHROPIC_BASE_URL globally to point at Moonshot,
        which meant a concurrent Anthropic request silently went to Moonshot too.
        Here the endpoint rides in the per-request config.
        """
        s = self.settings
        spec, model, search_api = self.resolve(options)
        qualified = spec.qualified(model)
        api_key = api_key_for(spec, s)

        configurable: dict[str, Any] = {
            "thread_id": job_id,
            # One model for every role; override per role if you need to split cost.
            #
            # The *_model_provider fields are NOT optional here. They default to
            # "openai" upstream and are passed explicitly into .with_config(),
            # which overrides the "<provider>:" prefix on the model string. Leave
            # them unset and every request silently routes to OpenAI no matter
            # which provider was selected.
            "research_model": qualified,
            "research_model_provider": spec.lc_provider,
            "research_model_max_tokens": 8_000,
            "final_report_model": qualified,
            "final_report_model_provider": spec.lc_provider,
            "final_report_model_max_tokens": 16_000,
            "compression_model": qualified,
            "compression_model_provider": spec.lc_provider,
            "compression_model_max_tokens": 8_000,
            "summarization_model": qualified,
            "summarization_model_provider": spec.lc_provider,
            "summarization_model_max_tokens": 8_000,
            # credentials + endpoint, per request
            "user_api_key": api_key,
            "model_base_url": spec.base_url,
            "tavily_api_key": s.tavily_api_key,
            # behaviour
            "search_api": search_api,
            "allow_clarification": (
                s.allow_clarification
                if options.allow_clarification is None
                else options.allow_clarification
            ),
            "max_structured_output_retries": s.max_structured_output_retries,
            "max_researcher_iterations": (
                options.max_researcher_iterations or s.max_researcher_iterations
            ),
            "max_react_tool_calls": (
                options.max_react_tool_calls or s.max_react_tool_calls
            ),
            "max_concurrent_research_units": (
                options.max_concurrent_research_units or s.max_concurrent_research_units
            ),
        }
        return {"configurable": configurable, "recursion_limit": 50}

    # -- execution ------------------------------------------------------

    async def stream(
        self,
        query: str,
        options: ResearchOptions,
        job_id: str,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[tuple[Event, ResearchResult | None], None]:
        """Yield `(event, result_or_None)`; the final tuple carries the result."""
        # imported lazily so that importing this module (tests, CLI, MCP schema
        # dumps) does not pull in the whole LangChain stack
        from langchain_core.messages import HumanMessage

        _ensure_vendor_on_path()
        from open_deep_research.deep_researcher import deep_researcher
        from open_deep_research.state import AgentInputState

        spec, model, search_api = self.resolve(options)
        config = self.build_config(options, job_id)
        timeout = options.timeout_seconds or self.settings.research_timeout_seconds

        result = ResearchResult()
        seq = 0
        started = time.monotonic()
        stage = ResearchStage.INITIALIZATION
        stage_started = started
        seen_urls: set[str] = set()

        def emit(
            etype: EventType,
            content: str | None = None,
            *,
            st: ResearchStage | None = None,
            data: dict[str, Any] | None = None,
        ) -> Event:
            nonlocal seq
            seq += 1
            return Event(
                type=etype,
                job_id=job_id,
                stage=st or stage,
                content=content,
                sequence=seq,
                data=data or {},
            )

        yield emit(
            EventType.STAGE_START,
            f"Researching: {query}",
            data={
                "provider": spec.id,
                "model": model,
                "search_api": search_api,
                "timeout_seconds": timeout,
            },
        ), None

        stream = deep_researcher.astream(
            AgentInputState(messages=[HumanMessage(content=query)]),
            config=config,
            stream_mode="updates",
        )

        try:
            while True:
                if cancel_event and cancel_event.is_set():
                    await stream.aclose()
                    raise ResearchCancelled()

                elapsed = time.monotonic() - started
                remaining = timeout - elapsed
                if remaining <= 0:
                    await stream.aclose()
                    raise ResearchTimeout(f"exceeded {timeout}s")

                try:
                    # bounded wait so cancellation and timeout stay responsive
                    # even while the graph is blocked on a slow model call
                    chunk = await asyncio.wait_for(
                        stream.__anext__(), timeout=min(remaining, 20.0)
                    )
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    yield emit(
                        EventType.HEARTBEAT,
                        data={"elapsed_seconds": round(time.monotonic() - started, 1)},
                    ), None
                    continue

                for node, payload in (chunk or {}).items():
                    new_stage = _NODE_STAGE.get(node)
                    if new_stage and new_stage != stage:
                        now = time.monotonic()
                        result.stage_timings.append(
                            StageTiming(stage=stage, seconds=round(now - stage_started, 3))
                        )
                        yield emit(EventType.STAGE_END, st=stage), None
                        stage, stage_started = new_stage, now
                        yield emit(EventType.STAGE_START, node), None

                    for event in self._events_from_payload(
                        node, payload, emit, result, seen_urls
                    ):
                        yield event, None

        except (ResearchCancelled, ResearchTimeout):
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller as a job error
            logger.exception("research graph failed for job %s", job_id)
            raise RuntimeError(str(exc)) from exc

        result.stage_timings.append(
            StageTiming(stage=stage, seconds=round(time.monotonic() - stage_started, 3))
        )
        yield emit(
            EventType.JOB_SUCCEEDED,
            st=ResearchStage.COMPLETE,
            data={"duration_seconds": round(time.monotonic() - started, 2)},
        ), result

    # -- payload -> events ----------------------------------------------

    def _events_from_payload(
        self,
        node: str,
        payload: Any,
        emit,
        result: ResearchResult,
        seen_urls: set[str],
    ) -> list[Event]:
        """Translate one graph update into public events, accumulating the result."""
        events: list[Event] = []
        if not isinstance(payload, dict):
            return events

        if brief := payload.get("research_brief"):
            result.research_brief = str(brief)
            events.append(emit(EventType.THINKING, str(brief)[:2000]))

        if report := payload.get("final_report"):
            result.report_markdown = str(report)
            events.append(emit(EventType.REPORT_CHUNK, str(report)))

        for note in payload.get("notes", []) or []:
            text = str(note)
            self._harvest_sources(text, result, seen_urls, events, emit)

        for message in payload.get("messages", []) or []:
            content = getattr(message, "content", None)
            calls = getattr(message, "tool_calls", None) or []
            for call in calls:
                name = call.get("name") if isinstance(call, dict) else None
                if name:
                    result.usage.tool_calls += 1
                    if "search" in name.lower():
                        result.usage.searches += 1
                    events.append(
                        emit(EventType.TOOL_CALL, name, data={"tool": name})
                    )
            if isinstance(content, str) and content.strip():
                self._harvest_sources(content, result, seen_urls, events, emit)
                if node in ("supervisor", "researcher", "research_supervisor"):
                    events.append(emit(EventType.THINKING, content[:2000]))

        return events

    @staticmethod
    def _harvest_sources(
        text: str,
        result: ResearchResult,
        seen: set[str],
        events: list[Event],
        emit,
    ) -> None:
        found = [u.rstrip(".,;") for u in _URL_RE.findall(text)]
        fresh = [u for u in found if u not in seen]
        if not fresh:
            return
        for url in fresh:
            seen.add(url)
            result.sources.append(Source(url=url))
        events.append(
            emit(
                EventType.SOURCES,
                f"{len(fresh)} new source(s)",
                data={"urls": fresh, "total": len(seen)},
            )
        )
