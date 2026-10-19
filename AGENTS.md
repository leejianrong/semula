# AGENTS.md

## What

semula is a record-replay fake LLM server: an OpenAI-compatible endpoint that replays recorded answers and, in record mode, parks unknown prompts for a coding agent to answer over MCP or CLI. Python 3.11+, FastAPI, SQLite, `uv`. src layout in `src/semula/`.

Status: pre-alpha. Planning is done; implementation starts at slice SEM-E2. Trust the code over any doc where they disagree, and fix the doc.

## Why

Cut LLM API cost in dev and test by turning a coding-agent subscription into a recorder. The load-bearing choices are in `docs/adr/` (read 0002 on the record queue and 0003 on matching before touching those areas). Decisions and their status: `QUESTIONS.md`. Plan: `PLAN.md`.

## How

- Setup: `uv sync --extra dev` (or `make setup`), then `make hooks` once.
- Gate before pushing: `make check` (ruff, mypy strict, fast tests). `make help` lists everything.
- Fast layer = tests marked `unit` or `integration`: no network, no Docker, temp SQLite only. `e2e` boots the real container and runs in CI only. Never let a slow test into the fast layer.
- Work is tracked on Pandan board **SEM** (board 39, use the `pandan` skill). Pick a card, branch per slice off fresh `main`, land via PR with green CI. No direct pushes to `main`.
- Every bug or flake gets a failing test first, then the fix.
- All surfaces (REST, CLI, MCP, dashboard) call one service layer; never write the database from a surface (ADR-0005).
- Add a runtime dependency only in the slice that first needs it, and check its licence and offline behaviour.
- Never commit secrets, `.env`, or a `data/` directory. Recorded prompts can contain secrets, so never put real recordings in fixtures.
- The dashboard is built on indah (`/home/jian/projects/abang-ai/indah`) after the headless core. Needs for indah go in `docs/INDAH-REQUESTS-PROMPT.md`; do not file indah cards without asking.
