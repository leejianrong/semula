# ADR-0006: Headless core first; dashboard on indah, re-decided before hosting

- Status: Accepted
- Date: 2026-10-19
- Deciders: Jian (owner)

## Context

The value is in record and replay; the dashboard is a convenience. indah (the owner's own UI framework) is a candidate but lacks live push from outside a session, editor and diff components, routing and more. Hosted needs identity, login and hardening that indah does not have today. The owner wants the dashboard designed freely, not shaped by indah's limits.

## Decision

Build the headless core (API, replay, record, CLI, MCP) first. Design the dashboard freely and save it in `docs/design/`. Every gap versus indah becomes a request to the indah team (see `docs/INDAH-REQUESTS-PROMPT.md`), so indah improves. The MVP dashboard runs on indah plus those enhancements, mounted at `/ui`. Before the hosted version, decide again: stay on indah or move to Svelte + Vite + TypeScript. The dashboard only uses `/api/v1`, so either choice leaves the service unchanged.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Build the dashboard first | Delays the cost-saving core and blocks on indah. |
| HTMX and Jinja inside FastAPI | Suggested in the first chat; set aside because the owner wants indah to grow through real use. |
| Svelte + Vite + TypeScript now | Adds a Node build to a local tool; kept as the likely hosted option. |

## Consequences

Buys early value and a real indah customer. Costs that the dashboard slices (SEM-E10) depend on indah delivering E1 and E2, and that screens may be rebuilt if hosted leaves indah.
