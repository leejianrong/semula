# ADR-0013: Record-through forwards a miss to a real provider, behind a budget

- Status: Accepted (2026-10-24, owner's brief for the Wavelength cards SEM-92 and SEM-93)
- Date: 2026-10-24
- Deciders: Jian (owner)
- Builds on: [ADR-0002](0002-record-mode-park-and-answer-queue.md), [ADR-0010](0010-hosted-auth-and-tokens.md), [ADR-0011](0011-workspace-tenancy-and-per-project-mode.md)

## Context

ADR-0002 parks a miss for an agent. That cannot capture a real model's play once and replay it, which is what the Wavelength team needs for evals. ADR-0002 listed forwarding to a paid provider as "may be offered later as an explicit option". This is that option. It spends someone's money, so the design is mostly about what it must not do.

## Decision

**Where the key lives.** `SEMULA_UPSTREAM_KEY` (and `SEMULA_UPSTREAM_URL`, default OpenAI's `/v1`; `SEMULA_UPSTREAM_TIMEOUT`) are server configuration read at start. The key is never put in the database, a project setting, an entry, the call log, the audit log, an export, an API or MCP response, or a log line. `Settings.upstream_key` is `repr=False`. The request that goes upstream is the app's own JSON body with `Authorization: Bearer <key>`; none of the app's headers are copied. A provider's error text is shown (status plus message, 300 characters) only after the key and anything shaped like a credential (the redaction patterns of ADR-0007) are replaced with `[REDACTED]`, because providers do echo keys. An unreachable upstream says so by exception type only, never by URL.

**Opt-in, per project.** Three project settings, all "operator fields" (`OPERATOR_FIELDS`): `record_through` (default off), `upstream_model` (send this model instead of the request's), `upstream_max_calls` (default 100). Like `mode`, they are never exported, and importing a bundle resets them: whether a project spends money is a decision about this server. Turning `record_through` on needs the admin role (it is a project setting), is audited as a settings change, and is refused with 422 `upstream_not_configured` when the server has no key. A forward happens only when the project's own mode is `record`, `record_through` is on, and the caller is not a replay-scope token. Replay mode never forwards and still fails fast with 424. A forward is never a fallback inside replay.

**Hosted.** The key is the operator's, so a workspace admin may turn forwarding on only if the operator sets `SEMULA_UPSTREAM_TENANTS=on`; otherwise 403. Settings are project-bound like every other setting: a token bound to one project cannot read or change another's (the service methods are in the project-binding and tenant-isolation matrices). Forwarded calls appear in the call log as outcome `forwarded`; they are not audited one by one (the count in `upstream_usage` and the log are the record), only the setting change is.

**Budget (SEM-93).** One table, `upstream_usage(workspace_id, project_id, calls, prompt_tokens, completion_tokens)`, with row-level security like other tenant tables. A call is counted *before* it is made with a single guarded `UPDATE ... WHERE calls < cap`, so concurrent misses cannot pass the cap; a call that fails (no answer, error status, invalid answer) is taken back. Tokens are added from the answer's `usage` when it has one. When the cap is spent a miss fails with 429 `semula_upstream_budget`; recorded answers keep replaying. There is a finite default cap on purpose: forgetting to set one must not mean unlimited spend.

**One call per prompt.** Identical misses at once share one upstream call (single flight per workspace, project and fingerprint, inside the process); after it, the entry exists and later calls are ordinary hits. Replicas on one Postgres do not coordinate: two replicas can each forward the same new prompt once. The cap bounds it.

**What is saved.** The upstream answer goes through `parse_answer` and `validate_tool_calls` like an agent's, then becomes an ordinary entry (revision actor `agent`, summary "recorded from upstream"), so pinning, review and export work as before. A streamed request is forwarded as a stream with `include_usage` forced on, and saved with its chunks (ADR-0008); the app receives the answer after the upstream finishes, not as it arrives.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Take the key from the request (`Authorization` of the app) | Spreads a real credential to every app and test and invites it into logs and fixtures. |
| A global on/off switch | One project's capture should not let every project spend. |
| No default cap, opt in to a budget | A forgotten setting becomes an open tap. |
| Count spend from the call log | The log is pruned and written after the call, so it can neither stop a burst nor survive retention. |
| Pending row plus a worker that forwards | Reuses the queue but adds a moving part and a second state machine for the same answer. |

## Consequences

An operator can capture a real model into recordings with one environment variable, one setting per project, and a hard ceiling. Costs: a new table and migration (0018), a finite default cap people will hit and must raise, no price tables (calls and tokens only), and a second place (this) that must stay in step with how entries are saved (`_insert_entry`). Dry run (SEM-93) is a project setting, `upstream_dry_run`: with it on a miss fails 424 `semula_upstream_dry_run`, is logged as outcome `dry_run`, and is listed (distinct prompts with counts) by `GET /api/v1/projects/{id}/upstream`. The token cap, `upstream_max_tokens`, is checked before a call against tokens already spent, so a call can overshoot it once. Reset is an admin action and is audited (`project.upstream_reset`).
