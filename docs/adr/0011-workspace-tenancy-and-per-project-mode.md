# ADR-0011: Workspace tenancy: isolation, quotas, deletion, and mode per project

- Status: Accepted (2026-10-22, owner)
- Date: 2026-10-21
- Deciders: Jian (owner)
- Builds on: [ADR-0004](0004-sqlite-with-workspace-id-from-day-one.md), [ADR-0009](0009-sqlalchemy-core-alembic-postgres-path.md), [ADR-0010](0010-hosted-auth-and-tokens.md)

## Context

`workspace_id` is on every table, but its rules are not written down, and the code uses one workspace everywhere: `DEFAULT_WORKSPACE` appears 50 times in `service.py`. A hosted semula needs the rules, and three things found while drawing the hosted screens:

1. **Entry, pending, candidate and alias ids are global primary keys.** Children (`entry_revision`, `candidate`, `alias`, `entry_vector`) reference `entry.id` alone, so nothing in the database stops a child row from naming another workspace. Import keeps ULIDs, so importing one bundle into two workspaces collides on the key, and the error ("already exists in project X") leaks that another tenant has it.
2. **Mode is a process flag.** `Service.mode` is set from `SEMULA_MODE` or `PUT /api/v1/mode`, not stored, and per process. The drawn mode switch sits under the project switcher, so it is per project; two replicas would disagree about a global flag.
3. **Several things live only in process memory:** the live-log event bus, held-call wake-ups, the settings cache (1 s) and the embedding matrix.

## Decision

### 1. Who the tenant is

The workspace comes from the credential, never from the URL or a header alone. A token names its workspace; a session has an active workspace chosen from the person's memberships (the dashboard sends it, the server checks membership every time). Data-plane URLs stay as drawn (`/v1/projects/triage/...`): no workspace in them. Locally there is one workspace, `default`, and nothing changes.

The service takes a `Scope(workspace_id, principal)` as its first argument and passes it down; the 50 hard-coded constants go. A cross-tenant id is a **404**, the same as a missing one.

*Built in SEM-81 (2026-10-22), with one change of shape: instead of a `Scope` first argument on every method, `Service.for_scope(scope)` returns the service bound to that workspace (same database, caches and event bus), and the routes call it with `scope_of(request)`. A method cannot forget to pass the scope, and every query reads `self.workspace_id`. Events carry their `workspace` and live views and streams skip other workspaces'. Meaning-based matching is built only for the local workspace so far. `tests/integration/test_tenant_isolation.py` calls every public service method as workspace A with workspace B's ids and fails when a new method is not listed; it found and fixed one leak (a waiter could read another workspace's answer through a pending id).*

### 2. Isolation, in three layers

1. **Application:** every query filters by `scope.workspace_id`; a test builds two workspaces and calls every service method and every route with the other's ids, expecting 404 or an empty list. A new method without this test fails review.
2. **Database shape (the hosted migration; built in SEM-82, revision 0010, 2026-10-22: composite keys for six tables, SQLite rebuilt table by table and Postgres altered in place; Postgres RLS follows):** primary keys become `(workspace_id, id)` on `entry`, `pending`, `candidate` and `alias`; child foreign keys become composite (`(workspace_id, entry_id)`), so a child cannot name another tenant's parent and the same ULID can exist in two workspaces (which fixes import). Done before there is hosted data, while tables are small.
3. **Postgres row-level security as a backstop (built in SEM-82, revision 0011; `Database.for_workspace` sets the setting in every transaction; `FORCE` is not used, so the migrating owner role bypasses the policies and the application must connect as a role that does not own the tables):** each transaction runs `SET LOCAL semula.workspace_id = ...` and every tenant table has a policy `workspace_id = current_setting('semula.workspace_id')`. The application role is not a superuser and has no `BYPASSRLS`. A forgotten filter returns nothing instead of leaking. SQLite (local) is single-tenant and has no RLS.

### 3. The rule for every table

| Table | Tenant | Rule |
|---|---|---|
| `workspace` | is the tenant | Global row; `deleted_at` for the grace period (section 5) |
| `project` | `workspace_id` | PK `(workspace_id, id)`; slug unique per workspace, not globally |
| `entry` | `workspace_id` + project FK | PK `(workspace_id, id)`; always read by scope |
| `entry_revision` | `workspace_id` | Composite FK to `entry`; written with the parent in one transaction; cascades |
| `pending` | `workspace_id` + project FK | PK `(workspace_id, id)`; one open request per fingerprint is unique per project already |
| `candidate`, `alias` | `workspace_id` + project FK | Composite PK and FK to `entry`; cascade |
| `entry_vector` | `workspace_id` | Composite FK to `entry`; the in-memory matrix is keyed by workspace and project |
| `call_log` | `workspace_id` | No FK (cheap writes); deleted by project, workspace and retention, which becomes **per workspace** (today one global cap) |
| `entry_fts` (SQLite) / `tsvector` column (Postgres) | `workspace_id` | Every search filters it; the index interface already takes workspace and project |
| `membership`, `invitation`, `api_token`, `audit_log` (ADR-0010) | `workspace_id` | Composite FKs; `audit_log` rows survive project deletion |
| `user`, `identity`, `session` (ADR-0010) | **global** | Not tenant data: a person can belong to many workspaces. Allow-listed in the "every table carries `workspace_id`" test; reached only through `membership` |

### 4. Mode per project

*Built in SEM-73 (2026-10-22): a project's mode is stored in its settings and kept across restarts; `PUT /api/v1/mode` without a project moves the in-memory default and says `persisted: false`. A bundle never carries or sets a mode.*

- `mode` becomes a project setting (`replay` or `record`), in the existing `ProjectSettings` JSON, so it is validated, cached for 1 s, patched by `PATCH /api/v1/projects/{id}`, and stored.
- **Effective mode** = the project's own mode if set, else the process default. The process default is `SEMULA_MODE` (and `PUT /api/v1/mode`, which keeps working locally and sets that default). A project that names a mode keeps it. Local users see no change; hosted has no process default beyond `replay`.
- A replay-scope token never parks, whatever the project's mode (ADR-0010). Switching a project to record needs `editor`; it is audited and shown in the log (`call_log.mode` already stores the mode per call).
- `/health` stops reporting one mode when hosted; the dashboard reads the project's.

### 5. Deletion

- **Entry, project:** as today (cascades); project delete also removes its log rows and index entries in the same transaction.
- **Workspace:** admin-only, typed confirmation. It sets `deleted_at`: tokens and sessions stop working at once, the dashboard shows a banner, and an admin can still export or restore for **30 days**. A purge job then hard-deletes in dependency order (log, index, entries and their children, projects, memberships, tokens, the workspace). Audit rows are kept, with no content.
- **Person:** removing an account deletes `identity` and `session`, ends memberships, and replaces the person in `audit_log` with an opaque id. A workspace whose last admin leaves must name another or be deleted.
- Backups hold deleted data until they expire (35 days); this is stated in the terms.

### 6. Quotas and limits (defaults; plans can override)

| Limit | Default | Enforced | Error |
|---|---|---|---|
| Projects per workspace | 20 | create | 403 `quota_exceeded` |
| Entries per workspace | 50,000 | on write, same transaction | 403 `quota_exceeded` |
| Stored bytes per workspace (requests and answers) | 250 MB | on write | 403 `quota_exceeded` |
| Request body | 1 MB | at the edge | 413 |
| Parked calls per project / workspace | 50 / 200 | when parking | 429 `too_many_parked` |
| `record_timeout` | at most 15 min | at settings | 422 |
| Request rate per token | 20/s steady, burst 100 | gateway, per token | 429 with `Retry-After` |
| Call-log rows per workspace | 50,000, 30 days | retention | n/a |
| Members / tokens | 25 / 50 | create | 403 `quota_exceeded` |

Counters are kept per workspace in a small table updated in the same transaction as the write, so a replay hit never runs a count query and the hit-path budget (p95 15 ms) holds. Every hit writes a `call_log` row; at hosted volume that is the first write-amplification cost to watch (batching or sampling is a later option).

### 7. Sharing

None between workspaces in the first hosted build (deferred in QUESTIONS). Export and import is the way to copy a project: with composite keys the same bundle can be imported into any workspace. A shared or public cassette library needs its own ADR (ownership, redaction review, abuse).

### 8. Shared state across replicas

*Built in SEM-90 (2026-10-23): `cluster.PgBridge` (LISTEN/NOTIFY on a sender and a listener thread, an outbox so publishing never blocks), an `answered` event that releases held calls on other replicas, a one-second look-again in the held-call and pending waits, `SEMULA_MIGRATE=off` with `semula admin migrate`, and an advisory lock for starts that migrate together.*

- **Event bus and held-call wake-ups:** Postgres `LISTEN/NOTIFY`. An answer written on one replica notifies; the replica holding the call wakes its waiter, with a 1 s poll as fallback. The local in-process bus stays for SQLite.
- **Settings cache:** the 1 s TTL stays; a change is visible within a second everywhere (a mode switch included).
- **Embedding matrix:** rebuilt per replica from `entry_vector` when its set changes, as now.
- **Migrations:** run once before rollout, not by each replica at startup (advisory lock as a stop-gap).

## Alternatives considered

| Option | Why not |
|--------|---------|
| One database (or schema) per workspace | Strong isolation, but migrations, pooling and cross-workspace admin multiply by the number of workspaces; RLS gives most of the safety for one schema |
| Keep global ids, scope by filter only | One forgotten filter leaks; children can name foreign parents; import collides |
| Workspace in every URL | Tokens already name it; a second source of truth is a way to get it wrong |
| Mode as a column | Needs a migration and a second cache; the settings JSON already has validation, caching and a PATCH route |
| Hard-delete a workspace at once | Irreversible mistakes; 30 days is cheap |
| Per-request count queries for quotas | Would break the hit budget |

## Consequences

- A large mechanical refactor (the `Scope` argument) and one migration (composite keys, RLS policies, new tables) before the first hosted request; the cross-tenant test matrix is the guard and grows with every method.
- The "every table carries `workspace_id`" test gains an explicit allow-list (`workspace`, `user`, `identity`, `session`).
- A project's mode survives restarts and replicas; locally the old `PUT /mode` still works as a default.
- Quota numbers are starting points to tune against real use.
- Build slices (filed on the board, epic SEM-E11): `Scope` through the service with the cross-tenant matrix; composite-key and RLS migration; mode as a project setting (can ship locally first, no hosted dependency); per-workspace retention and counters; workspace deletion and purge; `LISTEN/NOTIFY` bus.
