# Host semula for a team

Locally, semula trusts whoever can reach its port. To share one server between teams or projects, turn on hosted mode: every call then carries an **API token**, and the token decides which workspace, which project and what it may do. This page is the operator's view. It covers what exists today; people signing in, roles and the hosted dashboard are still to come, so the list of [what is missing](#what-is-not-there-yet) is worth a read.

```bash
SEMULA_HOSTED=on SEMULA_DATABASE_URL=postgresql://... semula serve
```

In hosted mode the open dashboard is off, and `X-Semula-Actor` means nothing. `/health` stays open for load balancers.

## Workspaces and tokens

A **workspace** is a tenant: its projects, entries, review queue and log are invisible to every other workspace. The operator creates workspaces and mints the first tokens on the server, with `semula admin`, which works on the database directly:

```bash
semula admin workspace-create acme --name "Acme"
semula admin token-create --workspace acme --name ci-replay --scope replay
```

```
created token 01M5HYWQZQMM2EA9YRS2WEHM07 (replay, ci-replay)
smla_live_…
This is the only time the token is shown.
```

Only a hash of the token is stored, so a lost token cannot be recovered, only replaced. Every token starts with `smla_live_`, which secret scanners can find if one leaks, and semula redacts that pattern from any prompt it stores.

## Scopes

A token has one of three scopes, each including the ones above it, and may be limited to one project. A project-bound token reaches that project and nothing else: other projects, and the entries, pending calls and review candidates in them, answer `404` as if they did not exist, and listings (projects, pending, the log) hold only its own. It cannot create projects.

## MCP

A coding agent connects over MCP with the same token. Use a `record` token, bound to the project the agent works on:

```bash
claude mcp add --transport http semula https://semula.example.com/mcp/ \
  --header "Authorization: Bearer $SEMULA_TOKEN"
```

Each tool needs a scope. The agent's own work (`wait_for_pending`, `answer_pending`, `get_entry`, `upsert_entry`, `update_entry`, `test_match` and the reads) needs `record`. `delete_entry`, `approve_candidate`, `reject_candidate`, `set_mode`, `create_project`, `update_project_settings` and anything with `force` need `admin`, which a recording agent should not hold. MCP refuses any request that carries an `Origin` header, so a web page cannot drive it.

| Scope | May | May not |
|---|---|---|
| `replay` | `/v1`: get a recorded answer, list models. A miss is an error **even when the project is recording**: a replay token never parks a call. | anything under `/api` |
| `record` | everything in `replay`, plus the recording agent's work over `/api/v1`: list and answer pending calls, read, create and edit entries, read the log, `test_match`, read the review queue | force past a pin, delete, approve or reject, change settings or mode, import or export |
| `admin` | everything in its project or workspace, including force, delete, approve and reject, settings, mode, import and export | manage tokens (people only) |

The ceiling on `record` is deliberate. A recorded prompt is untrusted text that the recording agent reads, so a prompt-injected agent must not be able to destroy a human's pinned fix or approve a near match into serving.

Two more rules follow from the credential:

- **The actor comes from the token.** Every write by a token is an agent's, whatever header it sends, so pins protect human edits.
- **Tokens cannot manage tokens.** Minting, rotating and revoking need the operator's CLI (or, once it exists, a signed-in admin).

Try it:

```bash
curl http://localhost:8080/v1/models
```

```json
{"error": {"message": "Missing or invalid API token. Send 'Authorization: Bearer smla_live_...'.", "type": "authentication_error", "param": null, "code": "invalid_api_key"}}
```

A replay token asking for something it has not got fails fast, in a recording project too:

```json
{"error":{"message":"No recorded answer matches this request, and a replay token never parks a call.","type":"semula_error","param":null,"code":"semula_no_match"}}
```

A scope that is too low names what is needed:

```json
{"error": {"message": "This token has scope 'record'; this call needs 'admin'.", "type": "authentication_error", "param": null, "code": "insufficient_scope"}}
```

## Rotating and revoking

```bash
semula admin token-rotate --workspace acme 01M5HYWQZQMM2EA9YRS2WEHM07 --grace 86400
semula admin token-revoke --workspace acme 01M5HYWQZQMM2EA9YRS2WEHM07
```

Rotating mints a successor with the same name, scope and project, and the old token keeps working for the grace period (a day by default), so you can swap deployed configuration without an outage. Revoking is immediate in the process that revokes. A verified token is trusted for 30 seconds without asking the database, so **another replica can honour a revoked token for up to 30 seconds**. `semula admin token-list --workspace acme` shows when each token was last used and from which network (a /24 for IPv4, a /48 for IPv6, not a full address).

Failed attempts are rate limited per client address (20 a minute), and a blocked address waits even with a good token.

## The audit log

Every change that matters is written to an append-only log: who did it (the operator, a token or, later, a person), what, to which target, from which address, with which request id. It covers tokens (create, rotate, revoke), projects (create, rename, delete, settings, mode, import), deleting an entry, every forced write and forced delete, approving or rejecting a review candidate and creating a workspace.

```bash
semula audit --action project.delete
```

The log keeps ids, key names and counts, never a prompt, an answer or a secret: a settings change records *which* settings changed, not their values. A database trigger refuses every `UPDATE` and every `DELETE` of a row younger than a year, so nothing can rewrite history, and a row older than that is pruned. Reading it needs the operator (or a signed-in admin, later), not a token.

## Postgres and roles

Hosting wants Postgres. Every tenant table has a [row-level-security](../reference/configuration.md) policy on `workspace_id`, so a query that forgot its filter sees nothing of another workspace's. The role that owns the tables, and any superuser, bypasses it. Migrate as the owner, then run semula as a role that is neither.

## What is not there yet

- **People.** No sign-in, roles or invitations; the dashboard is off.
- **Quotas**, and deleting a whole workspace.
