"""Public request/response contracts.

These are the shapes every integration surface speaks -- REST, SSE, MCP and both
client SDKs. Changing one here changes all of them, which is the point.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator


def utcnow() -> datetime:
    """`datetime.utcnow()` is deprecated in 3.12+ and returns a naive value."""
    return datetime.now(UTC)


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}


class ResearchStage(str, Enum):
    INITIALIZATION = "initialization"
    CLARIFICATION = "clarification"
    RESEARCH_BRIEF = "research_brief"
    SUPERVISION = "supervision"
    RESEARCH_EXECUTION = "research_execution"
    COMPRESSION = "compression"
    FINAL_REPORT = "final_report"
    COMPLETE = "complete"


class EventType(str, Enum):
    JOB_ACCEPTED = "job.accepted"
    STAGE_START = "stage.start"
    STAGE_END = "stage.end"
    THINKING = "thinking"
    TOOL_CALL = "tool.call"
    SOURCES = "sources"
    REPORT_CHUNK = "report.chunk"
    JOB_SUCCEEDED = "job.succeeded"
    JOB_FAILED = "job.failed"
    HEARTBEAT = "heartbeat"


# --------------------------------------------------------------------------
# requests
# --------------------------------------------------------------------------

class ResearchOptions(BaseModel):
    """Per-request knobs. Every one falls back to a server default."""

    model_config = ConfigDict(extra="forbid")

    provider: str | None = Field(
        default=None,
        description="Provider id, e.g. anthropic | openai | moonshot | openrouter.",
    )
    model: str | None = Field(
        default=None,
        description="Model id within the provider. Omit to use the provider default.",
    )
    search_api: str | None = Field(
        default=None, description="Search backend: tavily | anthropic | openai | none."
    )
    allow_clarification: bool | None = Field(
        default=None,
        description=(
            "Ask a clarifying question before researching. Leave false for "
            "unattended/integration use -- there is nobody to answer it."
        ),
    )
    max_researcher_iterations: int | None = Field(default=None, ge=1, le=10)
    max_react_tool_calls: int | None = Field(default=None, ge=1, le=20)
    max_concurrent_research_units: int | None = Field(default=None, ge=1, le=10)
    timeout_seconds: int | None = Field(default=None, ge=30, le=3600)


class ResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., min_length=3, max_length=4000)
    options: ResearchOptions = Field(default_factory=ResearchOptions)
    callback_url: AnyHttpUrl | None = Field(
        default=None,
        description=(
            "If set, the completed job is POSTed here with an HMAC-SHA256 "
            "signature in the X-DeepResearch-Signature header."
        ),
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Opaque values echoed back on the job and the webhook.",
    )
    idempotency_key: str | None = Field(
        default=None,
        max_length=255,
        description="Replaying the same key returns the original job instead of a new one.",
    )
    context_job_ids: list[str] = Field(
        default_factory=list,
        max_length=5,
        description=(
            "Ids of earlier jobs whose reports should be given to this run as "
            "prior context, for follow-up questions. The service loads them from "
            "its own store, so you do not resend the reports. Explicit by design: "
            "you choose what carries over rather than the service guessing."
        ),
    )

    @field_validator("query")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("query cannot be blank")
        return v

    @field_validator("metadata")
    @classmethod
    def _bounded(cls, v: dict[str, Any]) -> dict[str, Any]:
        if len(v) > 32:
            raise ValueError("metadata is limited to 32 keys")
        return v


# --------------------------------------------------------------------------
# responses
# --------------------------------------------------------------------------

class Source(BaseModel):
    url: str
    title: str | None = None
    snippet: str | None = None


class StageTiming(BaseModel):
    stage: ResearchStage
    seconds: float


class Usage(BaseModel):
    """Observability the reference implementation tracked only as wall-clock."""

    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    searches: int = 0


class ContextRef(BaseModel):
    """A prior job actually used as context, and how much of it was included."""

    job_id: str
    query: str
    characters_used: int
    truncated: bool = False


class ResearchResult(BaseModel):
    report_markdown: str = ""
    truncated: bool = Field(
        default=False,
        description=(
            "True when the run hit the model's context limit and the report is "
            "based on partial findings. The job still succeeds -- check this "
            "before treating the report as complete."
        ),
    )
    research_brief: str | None = None
    sources: list[Source] = Field(default_factory=list)
    stage_timings: list[StageTiming] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    context_used: list[ContextRef] = Field(
        default_factory=list,
        description="Prior jobs fed into this run via context_job_ids.",
    )


class ResearchError(BaseModel):
    code: str
    message: str
    retryable: bool = False


class Job(BaseModel):
    id: str
    status: JobStatus
    query: str
    provider: str
    model: str
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    result: ResearchResult | None = None
    error: ResearchError | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    callback_url: str | None = None

    def public(self) -> Job:
        return self


class JobAccepted(BaseModel):
    """202 body. Deliberately small -- the caller polls or waits for the webhook."""

    id: str
    status: JobStatus = JobStatus.QUEUED
    created_at: datetime
    poll_url: str
    events_url: str


class Event(BaseModel):
    """One SSE frame / one streamed update."""

    type: EventType
    job_id: str
    stage: ResearchStage | None = None
    content: str | None = None
    sequence: int = 0
    timestamp: datetime = Field(default_factory=utcnow)
    data: dict[str, Any] = Field(default_factory=dict)

    def to_sse(self) -> str:
        return f"event: {self.type.value}\ndata: {self.model_dump_json()}\n\n"


class ProviderInfo(BaseModel):
    id: str
    label: str
    default_model: str
    context_window: int
    configured: bool
    native_search: list[str]
    notes: str = ""


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    service: str
    version: str
    environment: str
    providers_configured: list[str]
    search_configured: bool
    job_backend: str
    timestamp: datetime = Field(default_factory=utcnow)


class WebhookPayload(BaseModel):
    """Body POSTed to `callback_url`."""

    event: Literal["research.succeeded", "research.failed"]
    delivered_at: datetime = Field(default_factory=utcnow)
    job: Job
