# Security Policy

## Reporting a vulnerability

Please report security issues privately using GitHub's
[Report a vulnerability](../../security/advisories/new) form rather than opening
a public issue.

Include the affected version or commit, reproduction steps, and impact. Expect a
first response within a few days.

## Design notes worth knowing

**Provider credentials never reach the client.** Callers authenticate with a
scoped service key; the LLM keys live only in the server's environment.
Revoking a caller key does not touch provider billing. The upstream project this
one derives from posted the user's provider key from the browser on every
request -- if you are migrating from it, rotate those keys.

**Webhooks are signed.** `X-DeepResearch-Signature: t=<unix>,v1=<hmac-sha256>`
over `"<timestamp>.<raw body>"`. Always verify before trusting a callback: the
URL is public, so anyone can POST to it. The timestamp is inside the signed
material, so a captured body cannot be replayed with a fresh header. Both client
SDKs ship a `verify_webhook` helper.

**Production refuses to boot when misconfigured.** With
`ENVIRONMENT=production`, startup fails if `API_KEYS` is empty, `AUTH_DISABLED`
is true, `CORS_ORIGINS` contains `*`, or `WEBHOOK_SECRET` is missing. This is
deliberate: a loud failure beats a service that silently serves the internet.

**Rate limiting is a backstop, not a control.** The in-process limiter stops one
runaway caller exhausting the worker pool. With multiple replicas, enforce real
limits at your ingress.

## Scope

Research output is model-generated from web sources and is not verified. Treat
reports as untrusted input: do not render them as HTML without sanitising, and
do not feed them to downstream systems that execute their contents.
