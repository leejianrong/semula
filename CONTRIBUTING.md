# Contributing

semula is pre-alpha. Work is planned as slices in [`docs/SLICES.md`](docs/SLICES.md) and tracked on a Pandan board; ask before starting something that isn't there.

## Setup

```bash
uv sync --extra dev   # or: make setup
make hooks            # pre-push gate; bypass once with: git push --no-verify
make check            # lint, mypy strict, fast tests
```

`make help` lists every target.

## Workflow

- One branch per slice off fresh `main`; open a PR; CI must be green before merge. `main` is protected.
- Fix bugs test-first: write the failing test, then the fix.
- Keep tests in the right layer. `unit` and `integration` need no network or Docker; `e2e` is CI only.
- Update the README, `docs/PLAN.md` or an ADR in the same PR that changes what they describe.
- Decisions that are hard to reverse get a short ADR in `docs/adr/`.

## Security

Never commit secrets. Report vulnerabilities privately via GitHub security advisories rather than a public issue.
