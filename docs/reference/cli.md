# CLI

`semula` is both the server (`semula serve`) and a client for a running one. Every client command talks to the server at `--url` (default `http://127.0.0.1:8080`, or the `SEMULA_URL` environment variable). If the server has an API key, set `SEMULA_API_KEY`: the CLI reads it from the environment so the key never appears in a process list. On a [hosted](../guide/hosted.md#signing-in-from-the-cli) server, run `semula login` once instead: it stores a token (and the server's address, which then becomes the default for `--url`) in `credentials.json` in `$SEMULA_CONFIG_DIR`, else `$XDG_CONFIG_HOME/semula`, else `~/.config/semula`, readable by you only. `SEMULA_API_KEY` wins over a stored token, and `SEMULA_URL` or `--url` wins over a stored address.

In the container, run it with `docker exec semula semula ...`.

## Flags every command takes

| Flag | Meaning |
|---|---|
| `--url URL` | The server to talk to. Goes before the command: `semula --url http://host:8080 pending`. |
| `--json` | Print JSON, one document per command (`tail` prints one per line). Errors go to stderr as `{"error": {...}}`. |
| `--version` | Print the version and exit. |

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success. |
| `1` | The server is unreachable, or it failed. |
| `2` | Usage error, or a command this build leaves out. |
| `3` | Not found. |
| `4` | Rejected: invalid input, a version conflict or a pinned entry. Read stderr. |
| `5` | Authentication failed. |

## Commands

| Command | Arguments and flags | What it does |
|---|---|---|
| `semula serve` | `--host` `--port` | Run the server |
| `semula mcp` |  | Run an MCP server on stdio that talks to --url |
| `semula pending` | `--all` `--project` `--wait` | List parked requests waiting for an answer |
| `semula show` | `<id>` | Show one pending request in full |
| `semula conversations` | `--project` `--log` `--min-size` | Group parked or logged calls whose messages extend an earlier call's |
| `semula claim` | `--project` `--lease` `--worker` | Claim the oldest unclaimed parked request and print it in full |
| `semula answer` | `<id>` `--text` `--file` `--stdin` | Answer a pending request, releasing the held call |
| `semula project list` |  | List projects |
| `semula project create` | `<id>` `--name` | Create a project |
| `semula project delete` | `<id>` | Delete a project and all its entries |
| `semula project settings` | `<id>` `--set` | Show or patch matching settings |
| `semula project upstream` | `<id>` `--reset` | Show what a project has forwarded upstream |
| `semula entry list` | `<project>` `--q` `--limit` `--offset` `--pinned` / `--no-pinned` `--has-alias` / `--no-has-alias` `--tool-calls` / `--no-tool-calls` `--never-hit` / `--no-never-hit` `--sort` `--order` | List a project's entries (no bodies) |
| `semula entry show` | `<id>` | Show one entry in full |
| `semula entry history` | `<id>` `--version` | An entry's change history (newest first) |
| `semula entry add` | `<project>` `--prompt` `--request-file` `--system` `--model` `--text` `--response-file` `--stdin` `--version` `--pin` `--as-human` | Author an entry (a request and its answer) up front |
| `semula entry set` | `<id>` `--version` `--text` `--file` `--stdin` `--add` `--selection` `--pin` `--unpin` `--force` `--as-human` | Edit an entry (needs its current --version) |
| `semula entry delete` | `<id>` `--version` `--force` `--as-human` | Delete an entry (needs its current --version) |
| `semula log` | `--project` `--outcome` `--limit` `--stats` `--window` | Show recent calls, newest first |
| `semula tail` | `--project` `--count` | Follow calls live (one JSON object per line with --json) |
| `semula embeddings status` |  | Is it on, and how many entries are indexed |
| `semula embeddings fetch` |  | Download the model once (needs the extra and network) |
| `semula review list` | `--all` `--project` | List the review queue, best score first |
| `semula review show` | `<id>` | Show a candidate and the entry it resembles |
| `semula review approve` | `<id>` | Serve this prompt from the candidate entry from now on |
| `semula review reject` | `<id>` | Never pair this prompt with that entry |
| `semula export` | `<project>` `--dir` | Write a project's entries to a directory of JSON files |
| `semula import` | `<dir>` `--project` `--force` | Load an exported directory (safe to repeat) |
| `semula mode` | `<mode>` `--project` | Show or switch the mode (replay or record) |
| `semula login` | `--url` `--project` `--scope` `--wait` | Sign in to a hosted server by approving a code in your browser |
| `semula whoami` |  | Show the server, workspace and access this login has |
| `semula logout` |  | Revoke the stored token and forget it |
| `semula usage` |  | What the workspace holds against its limits |
| `semula audit` | `--limit` `--action` `--before` | Show the audit log, newest first (people and the operator) |
| `semula admin migrate` |  | Bring the database up to date (run once before replicas) |
| `semula admin workspace-create` | `<id>` `--name` | Create a workspace |
| `semula admin workspace-delete` | `<workspace>` `--confirm` | Delete a workspace: tokens stop at once, purged after 30 days |
| `semula admin workspace-restore` | `<workspace>` | Bring a deleted workspace back during its 30 days |
| `semula admin workspace-suspend` | `<workspace>` `--reason` | Refuse a workspace's tokens and sign-ins; its data stays |
| `semula admin workspace-resume` | `<workspace>` | Give a suspended workspace its tokens and sign-ins back |
| `semula admin workspace-purge` |  | Hard-delete every workspace whose 30 days are over |
| `semula admin signup-code-create` | `--uses` `--days` `--note` | Make an invite code for signing up |
| `semula admin signup-code-list` |  | List invite codes for signing up |
| `semula admin signup-code-revoke` | `<id>` | Revoke an invite code for signing up |
| `semula admin invite` | `--workspace` `--email` `--role` | Invite someone to a workspace (prints the link) |
| `semula admin members` | `--workspace` | List a workspace's members |
| `semula admin recount` | `--workspace` | Rebuild a workspace's quota counters from what is stored |
| `semula admin token-create` | `--workspace` `--name` `--scope` `--project` `--expires` | Mint an API token for a workspace (the secret is shown once) |
| `semula admin token-list` | `--workspace` | List a workspace's API tokens |
| `semula admin token-revoke` | `<id>` `--workspace` | Revoke an API token |
| `semula admin token-rotate` | `<id>` `--workspace` `--grace` | Mint a successor; the old token keeps working for a grace period |
| `semula test-match` | `--project` `--prompt` `--file` `--stdin` | Which entry would fire for this prompt, and why |

`pending --wait SECONDS` holds the request until a call is parked, for up to 60 seconds, and prints an empty list if none came. `entry add` exits `4` with the current version when an entry already matches; pass `--version` to replace it.

`semula admin ...` works on the server's own database, not over HTTP, so it must run where the server's `SEMULA_DATABASE_URL` or `SEMULA_DATA_DIR` is set. It is for the operator of a [hosted](../guide/hosted.md) server: creating workspaces and minting tokens.

!!! note "The standalone binary"
    The [CLI binary](../guide/install.md) has every command above except `serve`, `mcp`, `embeddings fetch` and `admin`.
