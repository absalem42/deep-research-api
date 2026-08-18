"""Token accounting: callers embedding this need to answer "what did that cost?"."""

from __future__ import annotations

from app.research import DeepResearchEngine
from app.schemas import ResearchResult


class FakeMessage:
    def __init__(self, usage=None, tool_calls=None, content=""):
        if usage is not None:
            self.usage_metadata = usage
        self.tool_calls = tool_calls or []
        self.content = content


class TestTokenAccounting:
    def _run(self, settings, payloads):
        engine = DeepResearchEngine(settings)
        result = ResearchResult()
        seen: set[str] = set()
        emit = lambda *a, **k: None  # noqa: E731 - events are irrelevant here
        for payload in payloads:
            engine._events_from_payload("researcher", payload, emit, result, seen)
        return result

    def test_accumulates_across_messages(self, settings):
        result = self._run(
            settings,
            [
                {"messages": [FakeMessage(usage={"input_tokens": 100, "output_tokens": 20})]},
                {"messages": [FakeMessage(usage={"input_tokens": 250, "output_tokens": 80})]},
            ],
        )
        assert result.usage.input_tokens == 350
        assert result.usage.output_tokens == 100

    def test_messages_without_usage_are_ignored(self, settings):
        result = self._run(settings, [{"messages": [FakeMessage(content="no usage here")]}])
        assert result.usage.input_tokens == 0

    def test_null_counts_do_not_crash(self, settings):
        """Some providers send the key with a null value."""
        result = self._run(
            settings,
            [{"messages": [FakeMessage(usage={"input_tokens": None, "output_tokens": 5})]}],
        )
        assert result.usage.input_tokens == 0
        assert result.usage.output_tokens == 5

    def test_tool_and_search_counts_still_work(self, settings):
        result = self._run(
            settings,
            [
                {
                    "messages": [
                        FakeMessage(
                            usage={"input_tokens": 10, "output_tokens": 1},
                            tool_calls=[{"name": "tavily_search"}, {"name": "think_tool"}],
                        )
                    ]
                }
            ],
        )
        assert result.usage.tool_calls == 2
        assert result.usage.searches == 1
        assert result.usage.input_tokens == 10
