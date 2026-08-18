"""Follow-up context via `context_job_ids`.

The deliberate design choice under test: the service is stateless about
conversation. It does not decide what to remember -- the caller names the prior
jobs, and the service loads those reports from the store it already has. That
keeps one source of truth for the conversation (the caller's) and avoids
resending 10-20k-token reports over HTTP on every follow-up.
"""

from __future__ import annotations

import pytest

from app.jobs import ContextJobUnusable
from app.research import DeepResearchEngine
from app.schemas import ResearchResult

from .conftest import TEST_KEY  # noqa: F401  (imported for parity with other suites)


class TestBuildMessages:
    def _engine(self, settings, budget: int | None = None):
        if budget is not None:
            settings.max_context_characters = budget
        return DeepResearchEngine(settings)

    def test_no_prior_context_is_just_the_question(self, settings):
        result = ResearchResult()
        messages = self._engine(settings).build_messages("what is MCP?", None, result)

        assert len(messages) == 1
        assert messages[0].content == "what is MCP?"
        assert result.context_used == []

    def test_prior_report_becomes_conversation_history(self, settings):
        """Q/A turns, because the brief-writer already renders message history."""
        result = ResearchResult()
        messages = self._engine(settings).build_messages(
            "expand on pricing",
            [("job_a", "compare vector DBs", "# Report\n\nPinecone costs...")],
            result,
        )

        assert [m.content for m in messages] == [
            "compare vector DBs",
            "# Report\n\nPinecone costs...",
            "expand on pricing",
        ]
        assert result.context_used[0].job_id == "job_a"
        assert result.context_used[0].truncated is False

    def test_multiple_priors_keep_chronological_order(self, settings):
        result = ResearchResult()
        messages = self._engine(settings).build_messages(
            "now compare them",
            [
                ("job_a", "first question", "first report"),
                ("job_b", "second question", "second report"),
            ],
            result,
        )

        assert [m.content for m in messages] == [
            "first question",
            "first report",
            "second question",
            "second report",
            "now compare them",
        ]
        assert [c.job_id for c in result.context_used] == ["job_a", "job_b"]

    def test_budget_truncates_and_says_so(self, settings):
        result = ResearchResult()
        engine = self._engine(settings, budget=20)
        messages = engine.build_messages(
            "follow up", [("job_a", "q", "x" * 500)], result
        )

        assert len(messages[1].content) == 20
        assert result.context_used[0].truncated is True
        assert result.context_used[0].characters_used == 20

    def test_oldest_context_is_dropped_first(self, settings):
        """Recent work is likelier to be relevant, and prior context competes
        with live findings for the same window."""
        result = ResearchResult()
        engine = self._engine(settings, budget=10)
        engine.build_messages(
            "follow up",
            [("job_old", "old q", "o" * 50), ("job_new", "new q", "n" * 50)],
            result,
        )

        # the newest prior consumed the whole budget; the older one was dropped
        assert [c.job_id for c in result.context_used] == ["job_new"]

    def test_question_is_always_last(self, settings):
        result = ResearchResult()
        messages = self._engine(settings, budget=5).build_messages(
            "the actual question", [("job_a", "q", "y" * 900)], result
        )
        assert messages[-1].content == "the actual question"


class TestContextResolution:
    async def _manager(self, settings):
        from app.eventbus import InMemoryEventBus
        from app.jobs import JobManager
        from app.store import InMemoryJobStore

        return JobManager(
            settings, DeepResearchEngine(settings), InMemoryJobStore(settings), InMemoryEventBus(settings)
        )

    async def _save(self, manager, job_id, status, report):
        from app.schemas import Job, utcnow

        now = utcnow()
        job = Job(
            id=job_id,
            status=status,
            query=f"query for {job_id}",
            provider="anthropic",
            model="claude",
            created_at=now,
            updated_at=now,
        )
        if report is not None:
            job.result = ResearchResult(report_markdown=report)
        await manager.store.save(job)
        return job

    async def test_resolves_a_succeeded_job(self, settings):
        from app.schemas import JobStatus

        manager = await self._manager(settings)
        await self._save(manager, "job_a", JobStatus.SUCCEEDED, "# Prior")

        resolved = await manager.resolve_context(["job_a"])
        assert resolved == [("job_a", "query for job_a", "# Prior")]

    async def test_empty_list_resolves_to_nothing(self, settings):
        manager = await self._manager(settings)
        assert await manager.resolve_context([]) == []

    async def test_missing_job_is_rejected(self, settings):
        manager = await self._manager(settings)
        with pytest.raises(ContextJobUnusable, match="not found"):
            await manager.resolve_context(["job_nope"])

    async def test_unfinished_job_is_rejected(self, settings):
        from app.schemas import JobStatus

        manager = await self._manager(settings)
        await self._save(manager, "job_running", JobStatus.RUNNING, None)
        with pytest.raises(ContextJobUnusable, match="running"):
            await manager.resolve_context(["job_running"])

    async def test_failed_job_is_rejected(self, settings):
        from app.schemas import JobStatus

        manager = await self._manager(settings)
        await self._save(manager, "job_bad", JobStatus.FAILED, None)
        with pytest.raises(ContextJobUnusable, match="failed"):
            await manager.resolve_context(["job_bad"])

    async def test_empty_report_is_rejected(self, settings):
        """Silently accepting this would run the follow-up with no context and
        still bill for it."""
        from app.schemas import JobStatus

        manager = await self._manager(settings)
        await self._save(manager, "job_blank", JobStatus.SUCCEEDED, "   ")
        with pytest.raises(ContextJobUnusable, match="empty report"):
            await manager.resolve_context(["job_blank"])


class TestApiSurface:
    def test_followup_accepts_a_prior_job(self, client, auth, stub_engine):
        first = client.post(
            "/v1/research", json={"query": "compare vector databases"}, headers=auth
        ).json()["id"]

        for _ in range(100):
            if client.get(f"/v1/research/{first}", headers=auth).json()["status"] == "succeeded":
                break

        response = client.post(
            "/v1/research",
            json={"query": "expand on pricing", "context_job_ids": [first]},
            headers=auth,
        )
        assert response.status_code == 202

    def test_unknown_context_job_is_422(self, client, auth, stub_engine):
        response = client.post(
            "/v1/research",
            json={"query": "a follow-up question", "context_job_ids": ["job_missing"]},
            headers=auth,
        )
        assert response.status_code == 422
        assert "not found" in response.json()["detail"]

    def test_more_than_five_context_jobs_is_rejected(self, client, auth, stub_engine):
        response = client.post(
            "/v1/research",
            json={"query": "a question", "context_job_ids": [f"job_{i}" for i in range(6)]},
            headers=auth,
        )
        assert response.status_code == 422

    def test_unfinished_context_job_is_422(self, client, app, auth, stub_engine):
        """A job referenced while still running cannot supply a report.

        The running job is written straight to the store rather than produced by
        a slow stub: sleeping a real 30s to hold a job in RUNNING made this the
        slowest test in the suite by an order of magnitude, and racy besides.
        """
        import anyio

        from app.schemas import Job, JobStatus, utcnow

        now = utcnow()
        running = Job(
            id="job_still_running",
            status=JobStatus.RUNNING,
            query="slow research run",
            provider="anthropic",
            model="claude",
            created_at=now,
            updated_at=now,
        )
        with anyio.from_thread.start_blocking_portal() as portal:
            portal.call(app.state.jobs.store.save, running)

        response = client.post(
            "/v1/research",
            json={"query": "premature follow-up", "context_job_ids": [running.id]},
            headers=auth,
        )
        assert response.status_code == 422
        assert "running" in response.json()["detail"]
