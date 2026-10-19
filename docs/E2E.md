# End-to-end walkthrough

A manual run of the whole tool as a user and as the coding agent, using the real container and
the real OpenAI SDK. Repeat it after large changes. Last run: 2026-10-20 on `main` at the E7 merge
(SQLite, then Postgres). Sample app: [`examples/triage/triage.py`](../examples/triage/triage.py), a
support-ticket bot with three calls per ticket (classify, a tool call, a streamed reply). Every
run injects a timestamp and a temp path into the system prompt, randomises `temperature` and
shuffles the tool list. The ticket holds a fake API key.

Setup: `SEMULA_HOST_PORT=18080 SEMULA_MODE=record docker compose -p semula-e2e up -d --wait semula`,
then `semula --url http://127.0.0.1:18080 project create triage`. The app reads `SEMULA_BASE`
(default `http://127.0.0.1:18080/v1/projects/triage`).

| # | Check | Result |
|---|---|---|
| A1 | Image builds; `/health` ok; runs as uid 10001; `/data` volume writable | pass |
| A2 | Mode and projects readable over the CLI and MCP (20 tools listed) | pass |
| B3 | First call parks and the app blocks; `semula pending` shows it | pass |
| B4 | `semula show` gives the full request; the fake key shows as `[REDACTED]` | pass |
| B5 | Answer over the CLI (text); over MCP: a tool call missing a required argument is rejected with the field and fix; the corrected one is accepted; a streamed answer with chunk boundaries and `{{uuid}}` over the CLI (stdin) | pass |
| B6 | The fake key appears nowhere in `semula.db` or its WAL (byte search) | pass |
| C7 | Replay: identical output, fresh `{{uuid}}` each run, 3-8 ms per hit, no pending | pass |
| C8 | New timestamp, temp path, temperature and tool order: still hits (normalised tier) | pass |
| C9 | No outbound network in replay | covered by `tests/e2e` (not re-run by hand) |
| D10 | One word changed: a miss with `nearest`, and a review candidate with its score | pass |
| D11 | Approve: that prompt then replays (alias tier); approving closes the other candidates for the request; a three-turn conversation needs one approval per turn | pass |
| E12 | `semula tail` shows each call within a second | pass |
| E13 | Export, destroy container and volume, import into a fresh server, replay; re-import changes nothing; aliases survive; the export holds no secret | pass |
| E14 | Restart keeps data; with `SEMULA_API_KEY`: `/health` open, `/v1`, `/api`, `/mcp` need the key, CLI and MCP work with it | pass |
| F15 | The same flow on the Postgres compose profile (import, replay, alias, near-miss candidates, row counts in Postgres) | pass |

## Findings
- **Fixed:** the review queue listed every conversation that merely contained a prompt's words,
  because all calls in a project share a system prompt (three candidates per miss, two of them
  junk). Candidates now need the same speakers in the same order.
- **Observed, left alone:** `semula pending --json` prints a bare list while `review list` and
  `entry list` print objects (tested and documented as is). A one-word change to a multi-turn
  conversation needs one approval per turn.
