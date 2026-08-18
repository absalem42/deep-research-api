# Frontend integration

This is the **integration layer**, not a full UI rewrite. Sean's
[frontend](https://github.com/ShenSeanChen/launch-DeepResearch-Frontend) is ~2,700
lines of working React; the sensible move is to keep his components and swap the
data layer underneath them.

## What's here

| File | Purpose |
|---|---|
| `app/api/research/route.ts` | Bare collection path: `POST` to start a job, `GET` to list. |
| `app/api/research/[...path]/route.ts` | Everything below it: job state, cancel, SSE. |
| `lib/proxy.ts` | Shared forwarding — attaches the service key server-side, pipes SSE through untouched. |
| `lib/deepResearch.ts` | Typed client: `startResearch`, `getJob`, `subscribeToJob`, `listProviders`. |
| `hooks/useResearch.ts` | React hook wrapping one run: submit, live stage/events, final report, cancel. |

## The security fix

The reference frontend stores provider keys in `localStorage`:

```ts
localStorage.setItem('dra_api_keys', JSON.stringify(updatedKeys))   // contexts/ResearchContext.tsx:104
```

…and posts them with every request (`components/ResearchInterface.tsx:238`).
Any XSS on that page walks away with the user's OpenAI/Anthropic billing
credentials, and every user needs their own paid key just to click the button.

Here, provider keys live only in the backend's environment. The browser talks to
a same-origin route that attaches a **service** key server-side — so the browser
holds nothing, and revoking a compromised key does not touch provider billing.

## Why the proxy is two files

A catch-all segment does not match its own parent, so `[...path]/route.ts` alone
never handles `POST /api/research` — that request 404s. The bare path therefore
gets its own `route.ts`, and both delegate to `lib/proxy.ts`. Being explicit
beats relying on optional-catch-all (`[[...path]]`) behaviour, which varies
between Next versions.

## Status

Done and verified against a running backend: the hook is wired through
`ResearchInterface.tsx`, the localStorage key handling is deleted, the
comparison tab is rebuilt on the job API, and `next build` + `tsc --noEmit`
both pass.

## Migrating the existing UI

1. Copy `app/api/`, `lib/deepResearch.ts` and `hooks/useResearch.ts` into the
   existing Next.js app.
2. Set server-side env (no `NEXT_PUBLIC_` prefix — that would ship it to the browser):
   ```
   DEEP_RESEARCH_URL=http://localhost:8080
   DEEP_RESEARCH_API_KEY=drk_...
   ```
3. In `ResearchInterface.tsx`, replace the `fetch(`${BACKEND_URL}/research/stream`)`
   block and its `api_key` payload with the hook:
   ```tsx
   const { run, status, stage, events, job, error, cancel } = useResearch();
   ```
   `job.result.report_markdown` and `job.result.sources` replace the manually
   accumulated stream state.
4. Delete the API-key UI and everything reading `dra_api_keys` —
   `ResearchContext.tsx` lines 85–122. Nothing in the browser needs a provider
   key any more.
5. `ModelComparison.tsx` calls `/research/compare`, which this backend does not
   implement. Either drop that tab or rebuild it as N parallel `POST /v1/research`
   calls and compare the returned `duration_seconds` / `stage_timings` — the
   comparison is a client-side concern, not an endpoint.

## Event types

`subscribeToJob` yields the same events as the REST SSE stream:

| Type | Meaning |
|---|---|
| `stage.start` / `stage.end` | Pipeline stage boundaries — drive the progress UI from these |
| `thinking` | Supervisor/researcher reasoning text |
| `tool.call` | A tool fired (`data.tool` names it) |
| `sources` | New URLs found (`data.urls`) |
| `report.chunk` | Final report markdown |
| `job.succeeded` / `job.failed` | Terminal |
| `heartbeat` | Keep-alive; the hook filters these out |

## Google Docs export

`lib/googleDocs.ts` from the reference frontend works unchanged — it talks to
Google's API directly from the browser using the user's OAuth token and never
touches this backend. Copy it across as-is.
