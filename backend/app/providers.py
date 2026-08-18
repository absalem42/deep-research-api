"""Provider-agnostic model registry.

Every provider is described by data, not by branching code. Adding one is a new
`ProviderSpec` entry -- no changes anywhere else.

Two things here are deliberate fixes to the reference implementation:

1. `base_url` travels *through the request config*, never through `os.environ`.
   The original set `os.environ["ANTHROPIC_BASE_URL"]` per request, so two
   concurrent requests on different providers raced and one got the other's
   endpoint.
2. Credentials are resolved server-side from `Settings`, so a caller never
   supplies an LLM key.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from .config import Settings


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    label: str
    # Prefix understood by langchain's `init_chat_model`.
    lc_provider: str
    default_model: str
    settings_key_attr: str
    base_url: str | None = None
    # Search backends this provider can drive natively, cheapest first.
    native_search: tuple[str, ...] = ()
    context_window: int = 128_000
    notes: str = ""
    aliases: tuple[str, ...] = field(default=())

    def qualified(self, model: str | None = None) -> str:
        """`init_chat_model` wants "<provider>:<model>"."""
        return f"{self.lc_provider}:{model or self.default_model}"


# Model ids intentionally live here and nowhere else, so a model bump is a
# one-line change reviewers can see.
PROVIDERS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        id="anthropic",
        label="Anthropic Claude",
        lc_provider="anthropic",
        default_model="claude-sonnet-4-20250514",
        settings_key_attr="anthropic_api_key",
        native_search=("anthropic", "tavily"),
        context_window=200_000,
        notes="Best overall in the reference benchmark: thorough reports, ~82s.",
        aliases=("claude",),
    ),
    ProviderSpec(
        id="openai",
        label="OpenAI",
        lc_provider="openai",
        default_model="gpt-4o",
        settings_key_attr="openai_api_key",
        native_search=("openai", "tavily"),
        context_window=128_000,
        notes=(
            "Reference benchmark saw GPT-5 return an empty report and GPT-4o ignore "
            "formatting instructions. Treat as fallback, not default."
        ),
    ),
    ProviderSpec(
        id="moonshot",
        label="Moonshot Kimi K2",
        lc_provider="anthropic",  # Anthropic-compatible wire format
        default_model="kimi-k2-0905-preview",
        settings_key_attr="moonshot_api_key",
        base_url="https://api.moonshot.ai/anthropic",
        native_search=("tavily",),
        context_window=128_000,
        notes="Strongest instruction-following in the reference benchmark; slowest.",
        aliases=("kimi",),
    ),
    ProviderSpec(
        id="openrouter",
        label="OpenRouter",
        lc_provider="openai",  # OpenAI-compatible wire format
        default_model="anthropic/claude-sonnet-4",
        settings_key_attr="openrouter_api_key",
        base_url="https://openrouter.ai/api/v1",
        native_search=("tavily",),
        context_window=200_000,
        notes="One key, ~300 models. Set `model` to any OpenRouter slug.",
    ),
    ProviderSpec(
        id="groq",
        label="Groq",
        lc_provider="groq",
        default_model="llama-3.3-70b-versatile",
        settings_key_attr="groq_api_key",
        native_search=("tavily",),
        context_window=128_000,
        notes="Fastest tokens/sec; weaker at long multi-step tool use.",
    ),
    ProviderSpec(
        id="google",
        label="Google Gemini",
        lc_provider="google_genai",
        default_model="gemini-2.0-flash",
        settings_key_attr="google_api_key",
        native_search=("tavily",),
        context_window=1_000_000,
    ),
    ProviderSpec(
        id="deepseek",
        label="DeepSeek",
        lc_provider="deepseek",
        default_model="deepseek-chat",
        settings_key_attr="deepseek_api_key",
        native_search=("tavily",),
        context_window=64_000,
    ),
)

_BY_ID: dict[str, ProviderSpec] = {}
for _spec in PROVIDERS:
    _BY_ID[_spec.id] = _spec
    for _alias in _spec.aliases:
        _BY_ID[_alias] = _spec


class UnknownProviderError(ValueError):
    pass


class ProviderNotConfiguredError(RuntimeError):
    """Provider exists but no server-side credential is present."""


def get_provider(provider_id: str) -> ProviderSpec:
    try:
        return _BY_ID[provider_id.lower().strip()]
    except KeyError:
        raise UnknownProviderError(
            f"Unknown provider {provider_id!r}. Known: {sorted({s.id for s in PROVIDERS})}"
        ) from None


def api_key_for(spec: ProviderSpec, settings: Settings) -> str:
    key = getattr(settings, spec.settings_key_attr, "") or ""
    if not key:
        raise ProviderNotConfiguredError(
            f"Provider {spec.id!r} has no credential. "
            f"Set {spec.settings_key_attr.upper()} in the environment."
        )
    return key


def configured_providers(settings: Settings) -> Iterable[ProviderSpec]:
    """Only providers that can actually run right now."""
    for spec in PROVIDERS:
        if getattr(settings, spec.settings_key_attr, ""):
            yield spec


def resolve_search_api(spec: ProviderSpec, settings: Settings, requested: str | None) -> str:
    """Pick a search backend the chosen provider can actually drive."""
    if requested:
        return requested
    preferred = settings.default_search_api
    if preferred in spec.native_search and (
        preferred != "tavily" or settings.tavily_api_key
    ):
        return preferred
    for candidate in spec.native_search:
        if candidate == "tavily" and not settings.tavily_api_key:
            continue
        return candidate
    return "none"
