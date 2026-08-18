# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2026-08-18

### Added

- **Follow-up questions.** `context_job_ids` on a request names earlier runs
  whose reports become prior context. The service loads them from its own store,
  so a 10-20k-token report is never resent over HTTP. `follow_up()` /
  `followUp()` in both SDKs; `context_job_ids` on the MCP `deep_research` tool.

  Deliberately explicit rather than a server-side conversation: the caller's
  agent framework or app already owns the thread, and a second memory here would
  be a second source of truth. Up to 5 jobs, capped by `MAX_CONTEXT_CHARACTERS`
  (default 24,000); oldest context is dropped first, and
  `result.context_used[].truncated` records it. A missing, unfinished or empty
  referenced job is a `422` rather than a silent run without the context asked
  for.

- **`result.truncated`.** True when a run hit the model's context limit and the
  report is built on partial findings. The job still succeeds, so callers must
  check it; the MCP server also warns the agent inline.

### Fixed

Three context-management defects in the vendored graph, all of which degraded a
run silently instead of failing it (`NOTICE`, patches e-g):

- The supervisor's error handler read `if is_token_limit_exceeded(...) or True:`.
  The `or True` made the test dead code, so any exception ended research and
  returned partial notes. Now only a real context overflow degrades; other errors
  propagate and the job fails visibly.
- Context windows came from a substring-matched table returning `None` for every
  OpenRouter slug and for Groq, Gemini and DeepSeek -- 5 of 7 providers. On
  overflow that made the *report body* an error string telling the user to edit
  `utils.py`. The window is now supplied per request from the provider registry.
- Truncation was invisible to callers. See `result.truncated` above.

### Changed

- 135 tests, up from 101.

## [1.0.1] - 2026-08-18

### Added

- Logo and square icon (`assets/`), used in the README header.
- "What is original here, and what is not" section in the README: a line-count
  breakdown of which code is vendored from LangChain, which is adapted from
  Sean Chen's frontend, and which is original.
- 12 tests for the MCP server. Nothing imported `mcp_server`, so CI could not
  catch a break there.

### Fixed

- Pinned `mcp>=1.9.4,<2.0.0`. mcp 2.x removes `Server.list_tools`, breaking
  `mcp_server/server.py` at import, and `langchain-mcp-adapters` pins `<2.0.0`
  independently. A green Dependabot PR proposing 2.0.0 would have merged
  silently.
- Lint now covers `mcp_server/`, which was unchecked.
- Bumped CI actions past the Node 20 deprecation.

### Changed

- Dependency updates across backend, frontend and SDK, all verified by CI on
  Python 3.11/3.12/3.13.
- Dependabot tuned: minor/patch grouped into one PR per ecosystem, majors
  ignored for typescript/eslint/next/react. Going public opened 18 PRs in an
  hour, two already failing.

## [1.0.0] - 2026-08-18

First release. A production deep-research agent service built around a job model
so other systems can call it.

### Added

- **Async job API.** `POST /v1/research` returns `202` with a job id; track by
  polling, the SSE stream, or a signed webhook. A run takes 30-120s, which is
  too long to hold a request open.
- **Signed webhooks.** HMAC-SHA256 over `"<timestamp>.<body>"`, with the
  timestamp inside the signed material so a captured body cannot be replayed.
  Exponential backoff on 5xx/429/network; no retry on 4xx.
- **Authentication.** Bearer / `X-API-Key` with constant-time comparison and
  multiple independently revocable caller keys.
- **Provider-agnostic registry.** Anthropic, OpenAI, Moonshot (Kimi),
  OpenRouter, Groq, Google, DeepSeek. Adding one is a `ProviderSpec` entry.
- **Redis job backend.** `JOB_BACKEND=redis` shares job records, event backlog
  and idempotency claims across replicas; events fan out over pub/sub so any
  replica can stream any job. In-memory remains the default.
- **MCP server.** Exposes research as native agent tools over stdio.
- **Client SDKs.** Python (sync + async) and TypeScript, both with webhook
  verification helpers.
- **Next.js frontend.** Live SSE progress, model comparison, Google Docs export,
  and a server-side proxy so the browser never holds a key.
- **Idempotency keys.** Replaying a key returns the original job instead of
  paying for the research twice.
- **Production config guards.** With `ENVIRONMENT=production`, startup fails if
  `API_KEYS` is empty, `AUTH_DISABLED` is true, `CORS_ORIGINS` contains `*`, or
  `WEBHOOK_SECRET` is missing.
- **Per-job usage accounting.** `usage` reports input/output tokens, tool calls
  and searches, so an embedding platform can bill or budget a run. Raw counts
  rather than a price: published rates change, and a stale hardcoded number is
  worse than none.
- **89 tests**, covering auth, config guards, providers, the job lifecycle,
  usage accounting, and multi-replica Redis behaviour. No API keys or network
  required.

### Fixed

Three defects in the upstream code this project vendors, all marked
`PATCH(deep-research)` and described in [NOTICE](NOTICE):

- **Every request routed to OpenAI regardless of the selected provider.** The
  four `*_model_provider` fields default to `"openai"` and are passed explicitly
  into `.with_config()`, overriding the `"<provider>:"` prefix on the model
  string. Surfaced only by running a live request and reading the 401 body.
- **`NameError` on the Tavily search path.** `get_api_key_for_model()` was
  called in `utils.py` but its only definition is commented out. Invisible when
  running provider-native search, fatal the moment you switch to Tavily.
- **Cross-request endpoint race.** `os.environ["ANTHROPIC_BASE_URL"]` was
  written per request, so two concurrent requests on different providers
  clobbered each other. The endpoint now travels in the per-request config.

### Security

- Provider credentials are server-side only. The reference implementation
  accepted the caller's LLM key in the request body and stored provider keys in
  browser `localStorage`; both are removed. If migrating from it, rotate those
  keys.
- `allow_origins=["*"]` with `allow_credentials=True` replaced by an explicit
  origin list, with the wildcard rejected outright in production.
- Container runs as a non-root user.

### Known limitations

- The Redis backend is covered by tests against `fakeredis`; it has not yet been
  exercised against a live Redis server.
- Job state is per-process with `JOB_BACKEND=memory` (the default): a restart
  loses in-flight jobs. Use `redis` for multi-replica deployments.
- A job in flight when its own replica dies is lost. Retry from the client using
  `idempotency_key`.

[1.1.0]: https://github.com/absalem42/deep-research-api/releases/tag/v1.1.0
[1.0.1]: https://github.com/absalem42/deep-research-api/releases/tag/v1.0.1
[1.0.0]: https://github.com/absalem42/deep-research-api/releases/tag/v1.0.0
