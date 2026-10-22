# AGENTS.md

## What

semula is a record-replay fake LLM server: an OpenAI-compatible endpoint that replays recorded answers and, in record mode, parks unknown prompts for a coding agent to answer over MCP or CLI. Python 3.11+, FastAPI, SQLite, `uv`. src layout in `src/semula/`.

Status: 0.1.0 released (2026-10-22: image on ghcr.io, CLI binaries, docs site at leejianrong.github.io/semula). Built: record/replay, streaming and tools, projects and entries, tiered matching with review, MCP/CLI/API key, observability, export/import, embeddings, SQLite and Postgres, mode per project, and the dashboard's live log and Pending screen (indah 0.4). In progress: the rest of the dashboard (SEM-E10: entries, editor, review, playground). Next: hosted semula (SEM-E11), designed in ADR-0010 (OIDC people, scoped API tokens, roles, audit) and ADR-0011 (tenancy, isolation, quotas, deletion), both accepted by the owner on 2026-10-22; build slices SEM-81 to SEM-91. See the Pandan board for live status. Trust the code over any doc where they disagree, and fix the doc.

## Why

Cut LLM API cost in dev and test by turning a coding-agent subscription into a recorder. The load-bearing choices are in `docs/adr/` (read 0002 on the record queue and 0003 on matching before touching those areas). Decisions and their status: `docs/QUESTIONS.md`. Plan: `docs/PLAN.md`.

## How

- Setup: `uv sync --extra dev` (or `make setup`), then `make hooks` once.
- Gate before pushing: `make check` (ruff, mypy strict, fast tests). `make help` lists everything.
- Fast layer = tests marked `unit` or `integration`: no network, no Docker, temp SQLite only. `e2e` boots the real container and runs in CI only. Never let a slow test into the fast layer. `perf` (`make bench`) holds the replay latency budget, `make image-check` the image budgets (Docker), and `embeddings` (`make test-embeddings`) the real model; each runs as its own CI job. Embedding code stays in `embeddings.py` and imports numpy and fastembed lazily, so the default install never loads them.
- Docs site: `docs/` is built with Zensical (`zensical.toml`; `make docs` fails on any warning, `make docs-serve` previews). The tutorial, guide and reference are the user docs; planning notes (`PLAN`, `SLICES`, `QUESTIONS`, `INDAH-*`, `design/`) are excluded from the site. Change a command, flag, MCP tool, route or setting and update `docs/reference/` in the same PR: `tests/unit/test_docs_reference.py` fails when the tables drift. Every code block in the tutorial was run against a container; keep it that way.
- Work is tracked on Pandan board **SEM** (board 39, use the `pandan` skill). Pick a card, branch per slice off fresh `main`, land via PR with green CI. No direct pushes to `main`.
- Every bug or flake gets a failing test first, then the fix.
- All surfaces (REST, CLI, MCP, dashboard) call one service layer; never write the database from a surface (ADR-0005).
- Database: SQLAlchemy Core + Alembic, SQLite locally and Postgres hosted (ADR-0009). Change `schema.py` and add an Alembic revision together (a test fails if they drift); no raw SQL in the service; keep handlers non-blocking. `make test-postgres` runs everything on Postgres (needs Docker).
- Add a runtime dependency only in the slice that first needs it, and check its licence and offline behaviour.
- Never commit secrets, `.env`, or a `data/` directory. Recorded prompts can contain secrets, so never put real recordings in fixtures.
- The dashboard is built on indah (`/home/jian/projects/abang-ai/indah`) after the headless core. Needs for indah go in `docs/INDAH-REQUESTS-PROMPT.md`; do not file indah cards without asking.
