<p align="center">
  <img src="assets/logo.svg" alt="Deep Research API" width="440">
</p>

<p align="center">
  <a href="https://github.com/absalem42/deep-research-api/actions/workflows/ci.yml"><img src="https://github.com/absalem42/deep-research-api/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-blue.svg" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/tests-135-brightgreen.svg" alt="135 tests">
</p>


**Add ChatGPT-style deep research to your agent or your product.**

Ask a question, get back a cited markdown report — researched by a supervisor
agent that breaks the question into sub-tasks, runs researchers in parallel, and
synthesises the findings. Self-hosted, so the reports and the bill are yours.

Built on [LangChain's open_deep_research](https://github.com/langchain-ai/open_deep_research)
graph and the architecture from [Sean Chen's walkthrough](https://www.youtube.com/watch?v=dw9Vkig47S0),
rebuilt around a job model so other systems can call it. Full credit in [NOTICE](NOTICE).

```
query → Clarifier → Research Brief → Supervisor ─┬→ Researcher ─┐
                                                  ├→ Researcher ─┼→ compress → Report
                                                  └→ Researcher ─┘
```

## Who this is for

### You are building an agent

Give it research as a native capability. Point any MCP client at it — Claude
Code, Claude Desktop, Cursor, or your own framework:

```bash
claude mcp add deep-research -- python -m mcp_server.server
```

Your agent gets `deep_research`, `deep_research_start`,
`deep_research_status` and `deep_research_providers`. Or call the API from any
framework, in three lines:

```python
from deepresearch import DeepResearchClient

client = DeepResearchClient("https://research.example.com", api_key="drk_...")
print(client.research("Compare vector databases for a 10M-doc corpus").report_markdown)
```

### You run a product and want a research feature

Deep research as a backend service your app calls. Submit a job, get a webhook
when the report is ready — no held-open connections, no long-running requests in
your own stack:

```bash
curl -X POST https://research.example.com/v1/research   -H "Authorization: Bearer $KEY"   -d '{"query": "...", "callback_url": "https://you.example.com/hooks/research",
       "metadata": {"user_id": "u_123"}}'
```

`metadata` comes back on the webhook, so you can attribute a run to the end user
who asked for it. `usage` reports input/output tokens per job, so you can bill or
budget it. `idempotency_key` makes retries free instead of double-charging.

For a live progress UI, stream `GET /v1/research/{id}/events` — that is what the
included Next.js frontend does, and you can lift its `useResearch` hook straight
into your own app.

### What you need to run it

One provider key (Anthropic, OpenAI, OpenRouter, Moonshot, Groq, Gemini or
DeepSeek), optionally a Tavily key for search, and Docker:

```bash
git clone https://github.com/absalem42/deep-research-api
cd deep-research-api && cp .env.example backend/.env   # add one provider key
docker compose up --build
```

Your users never need an API key of their own — the credentials stay on your
server.

## How it differs from the tutorial it builds on

The reference implementation is a great teaching repo, but it is not something
you can put behind a domain. Concretely, what changed:

| Reference | Here |
|---|---|
| Caller sends their LLM API key in the request body | Keys are server-side; callers get a scoped service key |
| No authentication | Bearer / `X-API-Key`, constant-time compare, multiple revocable keys |
| `allow_origins=["*"]` **with** `allow_credentials=True` | Explicit origin list; wildcard refused in production |
| `os.environ["ANTHROPIC_BASE_URL"]` written per request | Endpoint travels in the per-request config — no cross-request races |
| `get_api_key_for_model()` called but never defined | Fixed — it was a latent `NameError` on the Tavily path |
| SSE only; a dropped connection loses the run | Job model: poll, stream, or signed webhook |
| Metrics fall back to in-memory silently | Documented; retention + eviction are explicit |
| Container runs as root, deps reinstalled on every code edit | Non-root, multi-stage, cached dependency layer |
| No tests around the API surface | 135 tests covering auth, providers, jobs, usage, context handling, follow-ups, MCP, multi-replica Redis |
| 3 hardcoded providers | Data-driven registry: Anthropic, OpenAI, Moonshot, OpenRouter, Groq, Gemini, DeepSeek |

## Quick start

```bash
cd backend
python -m venv .venv && .venv/Scripts/activate   # Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt
cp ../.env.example .env    # then fill in one provider key + API_KEYS
python -m app.main
```

Open http://localhost:8080/docs.

## The API

Every integration surface speaks the same contract.

### Submit

```bash
curl -X POST http://localhost:8080/v1/research \
  -H "Authorization: Bearer $DEEP_RESEARCH_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "What changed in EU AI Act enforcement in 2026?"}'
```

```json
{
  "id": "job_3f8a...",
  "status": "queued",
  "poll_url": "http://localhost:8080/v1/research/job_3f8a...",
  "events_url": "http://localhost:8080/v1/research/job_3f8a.../events"
}
```

`202 Accepted`, not `200`. A run takes 30–120s — far too long to hold a request
open, and the single biggest reason the reference design does not survive
contact with a load balancer.

### Then pick how you find out it finished

**Poll** — `GET /v1/research/{id}`

**Stream** — `GET /v1/research/{id}/events` (SSE). Subscribing late replays the
backlog, so you see the whole run even if you connect halfway through.
Heartbeats every 15s stop proxies dropping the connection.

**Webhook** — pass `callback_url` and receive a signed POST:

```json
{ "query": "...", "callback_url": "https://you.example.com/hooks/research" }
```

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/research` | Start a run → 202 + job id |
| `GET` | `/v1/research` | List runs (`?status=`, `?limit=`) |
| `GET` | `/v1/research/{id}` | Job state + result |
| `DELETE` | `/v1/research/{id}` | Cancel |
| `GET` | `/v1/research/{id}/events` | SSE progress |
| `GET` | `/v1/models` | Providers, and which have credentials |
| `GET` | `/health` | Unauthenticated liveness/readiness |

### Options

```json
{
  "query": "...",
  "options": {
    "provider": "anthropic",
    "model": "claude-sonnet-4-20250514",
    "search_api": "tavily",
    "max_concurrent_research_units": 3,
    "timeout_seconds": 600
  },
  "metadata": { "tenant": "acme" },
  "idempotency_key": "daily-brief-2026-08-18"
}
```

`idempotency_key` makes retries safe — replaying a key returns the original job
instead of paying for the research twice.

## Follow-up questions

Pass the ids of earlier runs and their reports become prior context:

```json
{ "query": "expand on the pricing section", "context_job_ids": ["job_3f8a..."] }
```

The service loads those reports from its own store, so **you never resend a
10–20k-token report over HTTP**.

Deliberately explicit rather than a server-side conversation. Your agent
framework or your app already owns the thread; a second memory here would be a
second source of truth that disagrees with yours, and you would lose control
over what context a run actually used. Naming the jobs keeps that decision where
it belongs.

```python
first = client.research("compare vector databases for a 10M-doc corpus")
more  = client.follow_up(first, "expand on the pricing section")
```

```typescript
const first = await client.research("compare vector databases");
const more  = await client.followUp(first, "expand on the pricing section");
```

Notes on behaviour:

- Up to 5 context jobs, capped at `MAX_CONTEXT_CHARACTERS` (default 24,000)
  total. Prior context competes with live findings for the same window, so it is
  bounded rather than allowed to crowd out the research.
- Over budget, the **oldest** context is dropped first, and
  `result.context_used[].truncated` says so.
- A referenced job that is missing, unfinished, or has an empty report is a
  `422` — rejected up front rather than silently researched without the context
  you asked for.

## Knowing when a report is incomplete

`result.truncated` is `true` when the run hit the model's context limit and the
report is built on partial findings. The job still **succeeds** — check the flag
before treating a report as complete.

This exists because the upstream graph degraded silently here: any exception in
the supervisor ended research early and returned whatever notes existed, with
nothing to distinguish a thin report from a finished one. See [NOTICE](NOTICE),
patches (e)–(g).

## Verifying webhooks

The callback URL is public, so anyone can POST to it. **Always verify.**

Signature: `X-DeepResearch-Signature: t=<unix>,v1=<hmac-sha256>` over
`"<timestamp>.<raw body>"`. The timestamp is inside the signed material, so a
captured body cannot be replayed with a fresh header. Retries use exponential
backoff on 5xx/429/network, and stop on 4xx.

```python
from deepresearch import verify_webhook

@app.post("/hooks/research")
async def hook(request: Request):
    raw = await request.body()             # RAW bytes, not the parsed JSON
    if not verify_webhook(SECRET, raw, request.headers["X-DeepResearch-Signature"]):
        raise HTTPException(400, "bad signature")
```

## Connecting other things

### Your own agent framework

Use the REST API directly, or the SDK:

```python
from deepresearch import DeepResearchClient

client = DeepResearchClient("https://research.example.com", api_key="drk_...")
job = client.research("Compare vector databases for a 10M-doc corpus")
print(job.report_markdown)

for event in client.stream(client.start("...")):   # or watch it live
    print(event["type"], event.get("content"))
```

```typescript
import { DeepResearchClient } from "@absalem42/deep-research";

const client = new DeepResearchClient({ baseUrl, apiKey });
const job = await client.research("...");
```

### As an MCP tool

Any MCP client — Claude Code, Claude Desktop, Cursor — can call it as a native
tool. The MCP server is a *client of the HTTP API*, so limits and credentials
stay enforced in one place.

```bash
DEEP_RESEARCH_URL=http://localhost:8080 \
DEEP_RESEARCH_API_KEY=drk_... \
claude mcp add deep-research -- python -m mcp_server.server
```

Tools: `deep_research` (blocks until done), `deep_research_start` +
`deep_research_status` (fire and collect later), `deep_research_providers`.

### n8n / Zapier / cron

`POST /v1/research` with a `callback_url` pointing at your webhook node. No
polling, no held-open connections.

## Providers

Adding one is a `ProviderSpec` entry in `app/providers.py` — nothing else changes.

| id | Default model | Notes |
|---|---|---|
| `anthropic` | `claude-sonnet-4-20250514` | Best overall in the reference benchmark (~82s, thorough) |
| `openrouter` | `anthropic/claude-sonnet-4` | One key, ~300 models — set `model` to any slug |
| `moonshot` (`kimi`) | `kimi-k2-0905-preview` | Best instruction-following, slowest, cheapest |
| `openai` | `gpt-4o` | GPT-5 returned an **empty report** in the reference benchmark |
| `groq` | `llama-3.3-70b-versatile` | Fastest tokens/sec, weaker at long tool chains |
| `google` | `gemini-2.0-flash` | 1M context |
| `deepseek` | `deepseek-chat` | Cheap |

Set `DEFAULT_PROVIDER`; override per request with `options.provider`.

## Configuration

See [`.env.example`](.env.example). With `ENVIRONMENT=production` the service
**refuses to boot** if `API_KEYS` is empty, `AUTH_DISABLED` is true,
`CORS_ORIGINS` contains `*`, or `WEBHOOK_SECRET` is missing — a misconfigured
deploy fails loudly instead of serving traffic wide open.

```bash
python -c "import secrets; print('drk_'+secrets.token_urlsafe(32))"   # caller key
python -c "import secrets; print('whsec_'+secrets.token_urlsafe(32))" # webhook secret
```

## Testing

```bash
cd backend && .venv/Scripts/python -m pytest
```

Tests stub the graph, so they are fast and need no API keys or network. 135 tests.

## Deploying

```bash
docker compose up --build
```

Cloud Run:

```bash
gcloud run deploy deep-research \
  --source backend \
  --region europe-west2 \
  --timeout 900 \
  --memory 2Gi \
  --no-allow-unauthenticated \
  --set-secrets ANTHROPIC_API_KEY=anthropic-key:latest,API_KEYS=caller-keys:latest
```

`--timeout 900` matters: the default 300s kills long SSE streams mid-run.

### Scaling

Two job backends, chosen by `JOB_BACKEND`:

**`memory`** (default) — everything in-process. Simple, no dependencies, and
correct for a single container. A restart loses in-flight jobs.

**`redis`** — job records, event backlog and idempotency claims live in Redis, so
any replica can serve any job:

```bash
JOB_BACKEND=redis
REDIS_URL=redis://redis:6379/0
```

What that buys you:

- **Poll any replica.** A job submitted to replica A is readable on B.
- **Stream from any replica.** Events fan out over Redis pub/sub, so B can serve
  the SSE stream for a run executing on A.
- **Idempotency holds across replicas.** The claim is a `SET NX`, so two
  replicas receiving the same retry agree on one winner.
- **Cancellation finds the owner.** A `DELETE` can land anywhere; a control
  channel tells whichever replica is actually running the job to stop.
- **Expiry is Redis's job.** Every key carries a TTL of `JOB_RETENTION_SECONDS`.

Two details that are easy to get wrong and are handled here: a subscriber joins
the pub/sub channel *before* reading the backlog (otherwise events published in
between are lost), and the resulting overlap between backlog tail and live
channel head is de-duplicated by the monotonic `sequence` on each event.

Startup calls `PING`, so a bad `REDIS_URL` fails the deploy in ~2s instead of
producing a service that looks healthy and breaks on the first request.

Still true either way: **one uvicorn worker per container.** Scale by adding
containers. A job that is mid-flight when its own replica dies is lost — retry
from the client using `idempotency_key`.

## Layout

```
backend/
  app/
    config.py       settings + production guards
    providers.py    provider registry (data, not branches)
    security.py     auth, HMAC signing, rate limiting
    research.py     graph wrapper, event normalisation
    jobs.py         orchestration + webhook delivery
    store.py        job persistence (memory | redis)
    eventbus.py     event fan-out (memory | redis pub/sub)
    routes.py       HTTP surface
    main.py         app factory, middleware, lifespan
  vendor/
    open_deep_research/   LangChain's graph (MIT) + PATCH(deep-research) fixes
  mcp_server/       MCP stdio server
  tests/
clients/
  python/           deepresearch
  typescript/       @absalem42/deep-research
```

Vendor patches are all marked `PATCH(deep-research)` so a future upstream bump
is greppable.

## Contributing

Issues and pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md)
for setup and conventions. Security issues should go through
[SECURITY.md](SECURITY.md) rather than a public issue.

## What is original here, and what is not

Being precise about this, because "built on" can mean anything.

**The research brain is not ours.** The multi-agent graph — clarifier,
supervisor, researchers, compression, report generation, and the prompts that
drive them — is LangChain's
[open_deep_research](https://github.com/langchain-ai/open_deep_research),
vendored under `backend/vendor/` (~2,500 lines). We changed four things in it,
each marked `PATCH(deep-research)` and listed in [NOTICE](NOTICE); three of
those were bug fixes.

**The service around it is ours.** Roughly 4,900 lines: the job engine, provider
registry, authentication, signed webhooks, Redis backend, event normalisation,
MCP server, both client SDKs, and the whole test suite.

**The UI is shared.** The React shell, styling and Google Docs export are Sean
Chen's, from
[launch-DeepResearch-Frontend](https://github.com/ShenSeanChen/launch-DeepResearch-Frontend)
(~1,700 lines kept). The data layer underneath it is ours (~800 lines): the API
client, the `useResearch` hook, the server-side proxy, and a rewritten model
comparison tab.

| | Lines | Source |
|---|---:|---|
| Research graph | ~2,500 | LangChain, MIT — vendored, 4 marked patches |
| Backend service | ~2,460 | Original |
| Tests | ~1,020 | Original |
| Client SDKs | ~600 | Original |
| Frontend data layer | ~790 | Original |
| Frontend UI shell | ~1,720 | Sean Chen, MIT — adapted |

So: a little over half the code is original, and the half that is not is the
part that would be foolish to rewrite. The contribution is turning a research
*agent* into a research *service* — jobs, auth, webhooks, multi-provider,
multi-replica — plus fixing three real bugs found on the way.

## Licence

MIT — see [LICENSE](LICENSE). All upstream components are MIT; their copyright
notices are retained as that requires, in [NOTICE](NOTICE) and in
`backend/vendor/open_deep_research/LICENSE`.

If this is useful to you, Sean's original [walkthrough
video](https://www.youtube.com/watch?v=dw9Vkig47S0) is worth your time.
