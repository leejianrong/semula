# ADR-0009: SQLAlchemy Core and Alembic, SQLite locally and Postgres when hosted

- Status: Accepted
- Date: 2026-10-19
- Deciders: Jian (owner)
- Amends: [ADR-0004](0004-sqlite-with-workspace-id-from-day-one.md)

## Context

ADR-0004 chose SQLite and said the service layer hides it from callers. That was only half
true: the service held about 35 raw `sqlite3` queries, SQLite-only syntax (`strftime`
defaults, `PRAGMA`, `LIKE` case rules, ordering by timestamp), a single locked connection
called straight from async handlers, and a hand-rolled migration runner that could not run
on another database. A hosted version needs Postgres, with login, tokens and more than one
replica. Local use must keep SQLite with no extra setup. Every new query made the port more
expensive, so it was done before the BM25 work in SEM-E6, which adds the first SQLite-only
feature (FTS5).

## Decision

1. **SQLAlchemy Core, no ORM.** The schema is defined once in `semula/schema.py`. The
   service queries with Core expressions and contains no SQL strings. Timestamps are ISO-8601
   UTC strings set in Python and JSON is stored as text, so both backends behave alike.
2. **Alembic for migrations**, shipped inside the package and run at startup. A SQLite file
   made by the old runner (`schema_version`, version 2) is adopted by stamping it at the
   baseline; any other legacy version is refused with a clear message. A test fails if
   `schema.py` and the migrations disagree.
3. **Backend choice by URL.** `SEMULA_DATABASE_URL` (default: a SQLite file in the data dir).
   Postgres uses psycopg 3 through an optional extra (`uv sync --extra postgres`, or
   `--build-arg UV_EXTRAS="--extra postgres"` for the image).
4. **Database-enforced invariants, not process-local ones.** At most one open request per
   fingerprint is a partial unique index; multi-statement operations run in one transaction;
   entries are versioned. These hold across threads and replicas.
5. **Handlers do not block the event loop.** Async handlers run service calls in a
   threadpool. Waking a held call goes through the waiter's own event loop, so an answer may
   arrive on any thread.
6. **Both backends are tested.** The same unit, integration and e2e suites run on SQLite
   (default) and Postgres (CI job `postgres`, and `make test-postgres`). Each test gets its own
   SQLite file or Postgres schema.
7. **Lexical search is a seam.** SEM-E6's BM25 tier must sit behind a small interface
   with one implementation per backend (SQLite FTS5 now; Postgres `tsvector` when hosted). No
   FTS SQL outside that implementation. Built in SEM-E6 as `semula/lexical.py`: `Fts5Index`
   (virtual table from migration 0003) and a `ScanIndex` fallback for other databases; scoring is
   in the service (ADR-0003 amendment).

## Alternatives considered

| Option | Why not |
|--------|---------|
| Keep raw sqlite3 and port later | Cost grows with every query; BM25 would add SQLite-only SQL |
| SQLAlchemy ORM | Mapped classes add weight and lazy-loading surprises; our queries are few and explicit |
| A repository layer over raw SQL, one per backend | Two hand-written SQL dialects to keep in step, no migrations story |
| Postgres only | Breaks the zero-setup local container |
| Store JSON as JSONB on Postgres | Dialect-specific types for no current query; revisit if a query needs it |

## Consequences

- One query layer and one migration history for both backends, and a CI job that proves it.
- A new table or column needs a change in `schema.py` **and** an Alembic revision; the agreement
  test enforces it. Migrations that alter columns on SQLite need Alembic batch mode.
- Postgres is not the default dependency, so a hosted image must opt in.
- Two or more replicas still share state only through the database. In-process pieces remain:
  the server's default mode (per process; a project's own mode is stored, ADR-0011) and held-call wake-ups (a held call is woken only
  in the process that holds it; an answer written on another replica is seen when its waiter next
  checks, so cross-replica release needs a notification channel). Both are hosted-design items.
- A project's settings are cached in-process for one second (`SETTINGS_TTL`) to keep replay fast; a change is visible at once on the replica that made it and within a second on others. The event bus (live log) is likewise in-process: a hosted deployment needs a shared channel.
- Migrations run at startup. Several replicas starting together would race; hosted deployment
  should run `upgrade` once before rolling out (an advisory lock is a possible later addition).
- The `LIKE` text filter is now case-insensitive on both backends (`ILIKE`); on large Postgres
  tables it will want a trigram index.
