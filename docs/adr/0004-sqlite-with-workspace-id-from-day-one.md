# ADR-0004: SQLite storage with workspace_id on every table from day one

- Status: Accepted
- Date: 2026-10-19
- Deciders: Jian (owner)

## Context

Local use needs zero setup and a file the user can back up. A hosted version later needs tenancy. Retrofitting tenant columns into a populated schema is painful. Teams also want cassettes in git.

## Decision

Use SQLite (WAL) on a mounted volume as the source of truth, with numbered migrations and a `schema_version` table. Every table carries `workspace_id`, with one default workspace locally. Entities: workspace, project, entry, call_log, pending, candidate. IDs: slugs for projects, ULIDs for entries. `semula export` and `import` round-trip a project to a directory of JSON or YAML files with `format: semula/1`, preserving ULIDs. Build no auth now.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Postgres | Needs a service in a local container; no benefit at this scale. |
| Files only (YAML in git) | Poor for hit counts, the call log, FTS5 and concurrent writes; export covers the git need. |
| Add tenancy later | Costs a migration over real data for a column that is cheap today. |

## Consequences

Buys a trivial local story and a cheap path to hosted. Costs an unused column locally and care to keep queries scoped. Hosted will need a different database for scale; the service layer hides SQLite from callers.
