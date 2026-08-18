## What this changes

<!-- One or two sentences. Link the issue if there is one. -->

## Why

<!-- The problem being solved, not the diff restated. -->

## Checklist

- [ ] `cd backend && python -m pytest` passes
- [ ] `cd frontend && npx tsc --noEmit && npx next build` are clean
- [ ] New provider? Added as a `ProviderSpec` in `app/providers.py`, nothing else
- [ ] Touched `backend/vendor/`? Marked with `PATCH(deep-research)` and noted in `NOTICE`
- [ ] No credentials read from the ambient environment inside the graph
