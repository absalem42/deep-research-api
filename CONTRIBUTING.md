# Contributing

Thanks for taking a look. This is a small project, so the process is light.

## Getting set up

```bash
# backend
cd backend
python -m venv .venv
.venv/Scripts/activate          # Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt
cp ../.env.example .env         # fill in one provider key + API_KEYS
python -m app.cli check         # sanity-check the environment
python -m pytest

# frontend
cd ../frontend
npm install
cp .env.example .env.local
npm run dev
```

On Windows use `python`, not `python3` -- the latter is a Microsoft Store stub
that will not run.

## Before opening a PR

```bash
cd backend  && .venv/Scripts/python -m pytest      # all tests must pass
cd frontend && npx tsc --noEmit && npx next build  # both must be clean
```

Tests stub the research graph, so they need no API keys and make no network
calls. Keep it that way -- a test that needs a paid key is a test nobody runs.

## Conventions

- **Providers are data.** Adding one means a new `ProviderSpec` in
  `backend/app/providers.py` and nothing else. If you find yourself writing
  `if provider == ...` anywhere else, the registry is the right place instead.
- **Never read credentials from the ambient environment inside the graph.**
  They travel in the per-request config. This is not stylistic: the bug it
  prevents is two concurrent requests on different providers clobbering each
  other's endpoint.
- **Vendor patches are marked.** Any change under
  `backend/vendor/open_deep_research/` gets a `PATCH(deep-research)` comment
  explaining why, and a matching entry in `NOTICE`. That is what makes a future
  upstream bump tractable.
- **Comments explain why, not what.** Assume the reader can read the code.

## Reporting bugs

Include the provider and model, whether `JOB_BACKEND` is `memory` or `redis`,
and the `X-Request-Id` from the response header if you have it. Please redact
API keys before pasting logs.
