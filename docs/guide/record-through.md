# How to record through a real model

This guide shows you how to capture a real model's answers once and replay them for free. Use it when you want a provider's own output rather than a coding agent's. It spends real money, so set the call and token caps before you start.

Normally a miss waits for a coding agent. Instead, point semula at a provider and let one project forward its misses:

```bash
SEMULA_UPSTREAM_KEY=sk-... semula serve        # optionally SEMULA_UPSTREAM_URL for a compatible provider
semula project settings game --set '{"mode": "record", "record_through": true, "upstream_max_calls": 200}'
```

Copy `.env.example` to `.env` for the variables (it is git-ignored) and start with `uv run --env-file .env semula serve`. A miss in that project calls the provider once, saves the answer (chunks and usage if your app streams) and returns it. The same prompt replays from then on with no call. It only happens in `record` mode; replay mode fails a miss fast as always.

- The key lives only in the server's environment. It is never stored, logged, exported or shown in an error.
- `upstream_max_calls` (default 100) is a hard ceiling per project. A miss after it fails with `semula_upstream_budget`; recorded answers keep replaying. A failed call does not count.
- `upstream_max_tokens` caps prompt plus completion tokens too. Tokens are known after a call, so one call can overshoot; the next miss fails.
- Dry run first: `--set '{"upstream_dry_run": true}'` forwards nothing, fails each miss with `semula_upstream_dry_run`, and `semula project upstream <id>` lists the prompts that would have been called and how often. Turn it off (`null`) to forward for real.
- `semula project upstream <id>` shows calls and tokens against the caps; `--reset` counts from zero. The log page of the dashboard shows forwarded and upstream calls.
- `upstream_model` sends a different model upstream than the app asked for.
- Forwarding spends the operator's money. On a hosted server a workspace admin can turn it on only if the operator set `SEMULA_UPSTREAM_TENANTS=on`.

## Related

- [Configuration](../reference/configuration.md) lists `SEMULA_UPSTREAM_KEY` and `SEMULA_UPSTREAM_URL`.
- [Errors](../reference/errors.md) explains `semula_upstream_budget` and `semula_upstream_dry_run`.
