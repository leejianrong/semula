# ADR-0010: Hosted identity: OIDC for people, scoped API tokens for programs

- Status: Proposed
- Date: 2026-10-21
- Deciders: Jian (owner)
- Builds on: [ADR-0004](0004-sqlite-with-workspace-id-from-day-one.md), [ADR-0005](0005-one-service-layer-optimistic-concurrency.md), [ADR-0006](0006-headless-first-dashboard-on-indah.md)
- Companion: [ADR-0011](0011-workspace-tenancy-and-per-project-mode.md) (who owns what; per-project mode)

## Context

Locally semula trusts whoever can reach the port; `SEMULA_API_KEY` is one shared secret ([`auth.py`](../../src/semula/auth.py)). A hosted semula has people (using the dashboard) and programs (an app's OpenAI client replaying calls, a coding agent recording over MCP) in several workspaces. These are different problems: a person signs in, a program carries a long-lived credential. One thing is currently unsafe to carry over: `X-Semula-Actor: human` decides whether a write may pass a pin ([`api_admin.py`](../../src/semula/api_admin.py)), and any caller can send it.

The hosted screens are drawn ([`docs/design/`](../design/README.md): sign-in, tokens, members). MCP clients authenticate to remote servers with OAuth 2.1 or a bearer header; the artboard's connect snippet uses the header (`claude mcp add --transport http ... --header "Authorization: Bearer smla_..."`).

## Decision

### 1. People: OIDC relying party, no passwords, no authorization server

- Sign in with an external OpenID Connect provider (GitHub and Google first). Authorization code flow with PKCE; check `state`, `nonce`, the `iss` the response came from (against the provider we started with: the mix-up defence of RFC 9207), `aud`, and an exact-match redirect URI. Accept only a **verified** email.
- A person is identified by `(issuer, sub)`, never by email. Two providers do not merge by email; a person links a second provider while signed in.
- Per-workspace enterprise SSO ("Continue with SSO" on the artboard) is a later step: an admin configures an issuer, so discovery and JWKS fetches take a user-supplied URL and need SSRF controls (https only, no private or link-local addresses after DNS resolution, size and time caps). Not in the first hosted build.
- Sessions are server-side rows in the database (indah H4: any replica can serve them; sign-out and revocation are immediate). The cookie is opaque, `__Host-` prefixed, `Secure`, `HttpOnly`, `SameSite=Lax`, rotated at sign-in, with an idle timeout (12 h) and an absolute one (7 d).
- State-changing dashboard requests (the event and upload routes included) check `Origin` against the configured host and carry a per-session CSRF token. `SameSite` alone is not relied on.
- Invitations are single-use, expire in 7 days, name a role, and are accepted by a signed-in person whose **verified** email matches. An invitation is never an alternative way to sign in.

### 2. Programs: opaque, scoped, per-project API tokens

- Format `smla_live_<32 random base62 characters>` (about 190 bits). The fixed prefix lets secret scanners find leaked tokens, and `smla_` is added to the built-in redaction patterns so a token pasted into a prompt never reaches storage or a log preview.
- Stored as a SHA-256 hash (the secret is high-entropy, so a slow hash adds nothing) with a short display prefix. Shown once at creation (indah H6: reveal-once field). Never logged; `Authorization` is not stored (already so).
- A token belongs to one workspace and to **one project or all of them**. Scopes (the artboard's Access column), each including the ones above it:

| Scope | May | May not |
|---|---|---|
| `replay` | `/v1/*` in its project(s): get a recorded answer, list models. A miss is an error **even if the project is in record mode**: a replay token never parks a call. | anything under `/api` or `/mcp` |
| `record` | all of `replay`, **plus** park calls in a record-mode project, and over `/mcp` and `/api`: list and answer pending, read, create and edit entries, read the log, `test_match`, read candidates | force past a pin, delete, approve or reject candidates, change settings or mode, import, manage tokens |
| `admin` | everything in its project(s), including force, delete, approve and reject, settings, mode, import and export | create tokens or manage members (people only) |

  The ceiling on `record` is deliberate: a recorded prompt is untrusted text that the recording agent reads, so a prompt-injected agent must not be able to destroy a human's pinned fix or approve a near-match into serving.
- **Actor comes from the credential, not a header.** A session is `human`; a token is `agent`. `X-Semula-Actor` is honoured only in local mode (no credentials). Entry history, pins and forced-write logging use the derived actor.
- **Tokens cannot create tokens**, and a token cannot see or list other tokens. Minting needs a person who is `admin` in the workspace.
- Lookup: one indexed query by hash. The hit path may not pay it every time (budget p95 15 ms), so verified tokens are cached in process for 30 s keyed by hash; revoking in the same process drops the entry at once, on other replicas within 30 s. That bound is stated in the UI.
- Rotation: "rotate" mints a successor with the same name, scope and project and sets the old token's `expires_at` to now + a grace period (24 h by default), so deployed config can be swapped without an outage. Tokens may also carry an optional expiry. Revoke is immediate. `last_used_at` is written at most once a minute per token (never per request) with a coarse client address.
- Over `/mcp`: Streamable HTTP with the bearer token, `Origin` validated (DNS rebinding), the `project` argument of every tool checked against the token's project (outside it: 404, not 403, to hide existence), `list_projects` filtered. Tokens are opaque and only ever accepted by the semula that issued them; semula forwards nothing upstream, so there is no token passthrough.
- OAuth 2.1 for MCP (semula as a resource server publishing RFC 9728 metadata, tokens bound to it by audience, scope challenges) is deferred to when a client needs it. It would use an external authorization server issuing the same three scopes; the scope model does not change. RFC 9207 `iss` handling and client-id metadata documents are authorization-server and client duties, not semula's.

### 3. Roles for people (indah H3)

| | viewer | editor | admin |
|---|---|---|---|
| read entries, log, pending, candidates, stats | yes | yes | yes |
| answer, edit, pin, approve and reject, `test_match`, switch a project's mode | | yes | yes |
| force past a pin, delete entries, import, project settings | | | yes |
| create and delete projects, tokens, members, roles, workspace | | | yes |

The last admin cannot leave or be demoted. A person's role applies to every project in the workspace (per-project roles are not needed yet).

### 4. Audit

An append-only `audit_log` (workspace, time, principal kind and id, action, target, request id, client address). It records sign-in and sign-out, token create, rotate and revoke, invitations, membership and role changes, mode changes, settings changes, imports, deletes (entry, project, workspace) and every forced write. The data plane stays in `call_log`. Audit rows hold no prompts or secrets; they are kept for a year and cannot be edited through any surface.

### 5. Abuse limits that auth owns

Failed token attempts are rate-limited per client address (and the cheap 401 path is made constant-time with `hmac.compare_digest` on the hash). Per-token and per-workspace request limits, body-size caps and a cap on parked calls per project are quota items in ADR-0011.

## Threat model

Assets: recorded prompts (they hold secrets despite redaction) and answers (they feed other people's tests); tokens; sessions; the integrity of pinned human edits.

| # | Threat | Control |
|---|---|---|
| T1 | A token leaks (committed `.env`, shared CI log, pasted in a prompt) | `smla_` prefix for scanners; redaction of the pattern; scoped to one project and the least scope; last-used and revoke; rotation with grace; expiry option |
| T2 | Cross-tenant read or write (guessed or reused ULID, project slug, header) | Workspace comes from the credential only; every query is scoped; cross-tenant ids are 404; defence in depth in ADR-0011 |
| T3 | An agent claims to be human to pass a pin | Actor derived from the credential; header honoured locally only |
| T4 | Prompt injection: a recorded prompt tells the agent to force, delete or approve | `record` cannot force, delete, approve, reject or change settings; those need `admin` and are audited; forced writes are visible in history |
| T5 | CSRF against the dashboard | `Origin` check, per-session token, `SameSite=Lax`, no state change on GET |
| T6 | Session fixation or theft | Rotate at sign-in; `__Host-`, `Secure`, `HttpOnly`; server-side rows revocable at once; idle and absolute expiry |
| T7 | OIDC mix-up, replay or open redirect | PKCE, `state`, `nonce`, issuer check, exact redirect URIs, verified email only |
| T8 | Invitation hijack by email | Single use, expiry, accepted only by a verified matching email, never a login path |
| T9 | SSRF through a user-supplied SSO issuer | Deferred; when built: https only, post-resolution address checks, caps |
| T10 | DNS rebinding or cross-site calls to `/mcp` | `Origin` validation; bearer header (not a cookie) so a browser does not attach it |
| T11 | Resource exhaustion: parked calls, huge bodies, token guessing | Quotas and caps (ADR-0011); failed-auth rate limit; 190-bit tokens |
| T12 | Token stolen from the database | Only hashes are stored; no reversible secret |
| T13 | Confused deputy through MCP token passthrough | Opaque tokens accepted only by their issuer; nothing forwarded upstream |

Out of scope: a compromised workspace admin; a compromised host or database; prompt content that is itself malicious but handled only by humans.

## Maps to the indah request list

| Need | indah item | Status |
|---|---|---|
| Server-signed session bound to the person; the browser cannot pick another's | H1 identity in context | needed |
| OIDC login, cookie sessions, CSRF and Origin checks, sign-out | H2 | needed |
| Viewer, editor, admin; hide and guard by permission | H3 | needed |
| Sessions that survive restarts and span replicas | H4 external session store | needed (semula keeps sessions in its own database, so H4 is satisfied if indah's store seam can be backed by it) |
| Rate limits, connection caps, CSP | H5 hardening | semula can do rate limits itself at the gateway; CSP is indah's |
| Reveal-once secret field, token list with revoke and last used | H6 | needed |
| Run under a path prefix, real client address, audit hook | H7 | needed |

How well indah answers H1 to H3 and H6 decides the SEM-55 spike. Everything on the data plane (`/v1`, `/mcp`, the token and role checks, audit) belongs to semula and does not wait for indah.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Passwords and our own user store | Adds reset, breach and abuse handling for no benefit over OIDC |
| Self-hosted authorization server for everything | Large surface for a tool whose users already have GitHub or Google; deferred behind the same scopes |
| JWT access tokens | Cannot be revoked at once, and semula already has the database; opaque and hashed is simpler and safer |
| Shared per-workspace key (the local `SEMULA_API_KEY`) | No scope, no per-project limit, no revoke of one consumer, no attribution |
| Trust `X-Semula-Actor` | Spoofable: it gates pin protection |
| Per-project roles for people | No use case yet; roles are workspace-wide |

## Consequences

- Needs new tables (global: `user`, `identity`, `session`; workspace-scoped: `membership`, `invitation`, `api_token`, `audit_log`), and a middleware that turns a credential into a principal for every surface. The service takes that principal (ADR-0011), so REST, CLI, MCP and dashboard cannot drift.
- Local mode is unchanged: no credentials, workspace `default`, the optional shared key, the actor header.
- Revocation can lag 30 s on other replicas. Accepted and stated.
- `record` is intentionally less than what a local agent can do today. Local mode keeps its current power; the PLAYBOOK says which calls need `admin` hosted.
- Build slices (not filed yet): principal and middleware with tokens and scopes; people, OIDC and sessions; members, roles and invitations; audit; the MCP project binding; quotas (ADR-0011).
