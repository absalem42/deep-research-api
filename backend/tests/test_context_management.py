"""Context-management behaviour.

Three upstream defects lived here, all of which degraded a run silently rather
than failing it. These tests pin the fixes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.providers import PROVIDERS, get_provider
from app.research import DeepResearchEngine
from app.schemas import ResearchOptions, ResearchResult

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vendor"))


class TestContextWindowIsKnownForEveryProvider:
    """Upstream's MODEL_TOKEN_LIMITS is a substring match that misses most of
    our providers. When it returns None on a context overflow, the graph puts an
    error string in the *report body* telling the user to go edit utils.py.
    """

    def test_every_provider_declares_a_window(self):
        for spec in PROVIDERS:
            assert spec.context_window > 0, spec.id

    @pytest.mark.parametrize("provider", [s.id for s in PROVIDERS])
    def test_window_reaches_the_graph_config(self, settings, provider):
        for spec in PROVIDERS:
            setattr(settings, spec.settings_key_attr, "test-key")

        configurable = DeepResearchEngine(settings).build_config(
            ResearchOptions(provider=provider), "job_1"
        )["configurable"]

        assert configurable["model_context_window"] == get_provider(provider).context_window

    def test_upstream_table_would_have_missed_these(self):
        """Documents *why* the config value exists, and fails loudly if upstream
        ever fixes its table (at which point this indirection can be revisited).
        """
        from open_deep_research.utils import get_model_token_limit

        missed = [
            "openai:anthropic/claude-sonnet-4",  # any OpenRouter slug
            "groq:llama-3.3-70b-versatile",
            "google_genai:gemini-2.0-flash",
            "deepseek:deepseek-chat",
        ]
        assert all(get_model_token_limit(m) is None for m in missed)


class TestSupervisorErrorHandling:
    """Upstream read `if is_token_limit_exceeded(...) or True:` -- dead code that
    turned every exception into a silent early return with partial notes.
    """

    def _code_lines(self) -> str:
        """Source with comments stripped -- the fix is described in a comment
        that quotes the original, so a naive grep matches itself.
        """
        source = (
            Path(__file__).resolve().parent.parent
            / "vendor/open_deep_research/deep_researcher.py"
        ).read_text(encoding="utf-8")
        return "\n".join(
            line for line in source.splitlines() if not line.strip().startswith("#")
        )

    def test_the_or_true_is_gone(self):
        assert "or True" not in self._code_lines()

    def test_non_context_errors_propagate(self):
        """A real error must reach the caller, not become a thin report."""
        source = (
            Path(__file__).resolve().parent.parent
            / "vendor/open_deep_research/deep_researcher.py"
        ).read_text(encoding="utf-8")
        block = source[source.index("supervisor hit the context limit") :][:900]
        assert "raise" in block


class TestTruncationIsVisible:
    def test_result_defaults_to_not_truncated(self):
        assert ResearchResult().truncated is False

    def test_flag_is_set_from_graph_state(self, settings):
        engine = DeepResearchEngine(settings)
        result = ResearchResult()
        events: list = []

        emitted = engine._events_from_payload(
            "supervisor",
            {"context_truncated": True},
            lambda *a, **k: events.append((a, k)) or "e",
            result,
            set(),
        )

        assert result.truncated is True
        assert emitted, "truncation should also surface as an event"

    def test_normal_payload_leaves_flag_clear(self, settings):
        result = ResearchResult()
        DeepResearchEngine(settings)._events_from_payload(
            "supervisor", {"notes": ["something"]}, lambda *a, **k: "e", result, set()
        )
        assert result.truncated is False

    def test_state_schema_carries_the_field(self):
        from open_deep_research.state import SupervisorState

        assert "context_truncated" in SupervisorState.__annotations__
