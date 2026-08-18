"""Provider registry and per-request config building.

The concurrency test is the important one: it pins the fix for the reference
implementation's `os.environ["ANTHROPIC_BASE_URL"] = ...` per-request write.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from app.providers import (
    ProviderNotConfiguredError,
    UnknownProviderError,
    api_key_for,
    configured_providers,
    get_provider,
    resolve_search_api,
)
from app.research import DeepResearchEngine
from app.schemas import ResearchOptions


class TestRegistry:
    def test_lookup_by_id(self):
        assert get_provider("anthropic").id == "anthropic"

    def test_alias_resolves(self):
        # the video calls it "kimi"; the provider is Moonshot
        assert get_provider("kimi").id == "moonshot"

    def test_case_insensitive(self):
        assert get_provider("ANTHROPIC").id == "anthropic"

    def test_unknown_provider_raises(self):
        with pytest.raises(UnknownProviderError):
            get_provider("not-a-provider")

    def test_openrouter_is_openai_compatible_with_its_own_endpoint(self):
        spec = get_provider("openrouter")
        assert spec.lc_provider == "openai"
        assert spec.base_url == "https://openrouter.ai/api/v1"

    def test_moonshot_speaks_anthropic_wire_format(self):
        spec = get_provider("moonshot")
        assert spec.lc_provider == "anthropic"
        assert "moonshot.ai" in spec.base_url

    def test_qualified_name_format(self):
        assert get_provider("anthropic").qualified("claude-x") == "anthropic:claude-x"

    def test_missing_credential_raises(self, settings):
        with pytest.raises(ProviderNotConfiguredError, match="GROQ_API_KEY"):
            api_key_for(get_provider("groq"), settings)

    def test_configured_providers_reflects_env(self, settings):
        ids = {s.id for s in configured_providers(settings)}
        assert ids == {"anthropic", "moonshot", "openrouter"}


class TestSearchResolution:
    def test_explicit_request_wins(self, settings):
        spec = get_provider("anthropic")
        assert resolve_search_api(spec, settings, "anthropic") == "anthropic"

    def test_falls_back_when_tavily_key_absent(self, settings):
        settings.tavily_api_key = ""
        spec = get_provider("anthropic")
        # anthropic can search natively, so it should land there, not on tavily
        assert resolve_search_api(spec, settings, None) == "anthropic"

    def test_provider_without_native_search_and_no_key_gets_none(self, settings):
        settings.tavily_api_key = ""
        assert resolve_search_api(get_provider("groq"), settings, None) == "none"


class TestConfigBuilding:
    def test_credentials_come_from_server_not_caller(self, settings):
        engine = DeepResearchEngine(settings)
        config = engine.build_config(ResearchOptions(provider="anthropic"), "job_1")
        assert config["configurable"]["user_api_key"] == "sk-ant-test"

    def test_base_url_is_in_config_not_environment(self, settings):
        """The whole point of the patch."""
        engine = DeepResearchEngine(settings)
        before = os.environ.get("ANTHROPIC_BASE_URL")

        config = engine.build_config(ResearchOptions(provider="moonshot"), "job_1")

        assert config["configurable"]["model_base_url"] == "https://api.moonshot.ai/anthropic"
        assert os.environ.get("ANTHROPIC_BASE_URL") == before

    def test_anthropic_has_no_base_url_override(self, settings):
        engine = DeepResearchEngine(settings)
        config = engine.build_config(ResearchOptions(provider="anthropic"), "job_1")
        assert config["configurable"]["model_base_url"] is None

    def test_concurrent_requests_do_not_share_endpoints(self, settings):
        """Two providers at once must not clobber each other.

        Under the reference implementation this is exactly the case that broke:
        the Moonshot request set a global env var and the Anthropic request
        picked it up.
        """
        engine = DeepResearchEngine(settings)

        async def build(provider: str) -> str | None:
            await asyncio.sleep(0)  # force interleaving
            config = engine.build_config(ResearchOptions(provider=provider), "j")
            await asyncio.sleep(0)
            return config["configurable"]["model_base_url"]

        async def run():
            return await asyncio.gather(
                *[build("moonshot" if i % 2 else "anthropic") for i in range(20)]
            )

        results = asyncio.run(run())
        for i, base_url in enumerate(results):
            if i % 2:
                assert base_url == "https://api.moonshot.ai/anthropic"
            else:
                assert base_url is None

    def test_options_override_server_defaults(self, settings):
        engine = DeepResearchEngine(settings)
        config = engine.build_config(
            ResearchOptions(provider="anthropic", max_react_tool_calls=11), "job_1"
        )
        assert config["configurable"]["max_react_tool_calls"] == 11

    def test_model_override(self, settings):
        engine = DeepResearchEngine(settings)
        config = engine.build_config(
            ResearchOptions(provider="openrouter", model="meta-llama/llama-3.3-70b"),
            "job_1",
        )
        assert config["configurable"]["research_model"] == "openai:meta-llama/llama-3.3-70b"

    @pytest.mark.parametrize(
        "provider,expected",
        [
            ("anthropic", "anthropic"),
            ("moonshot", "anthropic"),  # Anthropic-compatible wire format
            ("openrouter", "openai"),  # OpenAI-compatible wire format
        ],
    )
    def test_every_model_role_pins_the_provider(self, settings, provider, expected):
        """Regression: the four *_model_provider fields default to "openai"
        upstream and are passed explicitly into .with_config(), overriding the
        "<provider>:" prefix. Leaving any of them unset silently routes that
        role's calls to OpenAI. Caught live -- an anthropic run returned an
        OpenAI 401.
        """
        settings.moonshot_api_key = "sk-moon-test"
        engine = DeepResearchEngine(settings)
        configurable = engine.build_config(
            ResearchOptions(provider=provider), "job_1"
        )["configurable"]

        for role in ("research", "final_report", "compression", "summarization"):
            assert configurable[f"{role}_model_provider"] == expected, role

    def test_clarification_off_by_default(self, settings):
        """Unattended integrations have nobody to answer a clarifying question."""
        engine = DeepResearchEngine(settings)
        config = engine.build_config(ResearchOptions(provider="anthropic"), "job_1")
        assert config["configurable"]["allow_clarification"] is False
