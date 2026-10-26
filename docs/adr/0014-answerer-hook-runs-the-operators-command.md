# ADR-0014: An answerer hook runs the operator's command for each parked call

- Status: Accepted (2026-10-26, owner's brief for the Wavelength card SEM-94)
- Date: 2026-10-26
- Deciders: Jian (owner)
- Builds on: [ADR-0002](0002-record-mode-park-and-answer-queue.md) (the queue and its claim amendment), [ADR-0010](0010-hosted-auth-and-tokens.md), [ADR-0013](0013-record-through-and-its-budget.md)

## Context

ADR-0002 named "spawn `claude -p` or `codex exec` per miss" as an optional later adapter, off by default. The Wavelength team needs it for evals: one agent answering many parked calls in one context sees earlier prompts, which contaminates later answers, and it cannot run unattended. Each call needs a fresh answerer with no memory of the others.

This runs a program, or calls a URL, on the operator's behalf with text an app (or a user of the app) wrote. That is the most dangerous thing semula has been asked to do, so this ADR is mostly a threat model.

## Decision

**What it is.** A background worker in the semula process. For each parked call in a project that has the hook on, it starts one fresh process (or makes one HTTP request), sends the request on stdin (or as the body) as JSON, reads the answer from stdout (or the response), and answers the call through the same service layer as an agent (`answer_pending`: `parse_answer`, `validate_tool_calls`, quotas, an ordinary entry). Nothing it returns skips validation.

**Where the command comes from: server configuration only.** `SEMULA_ANSWERER_COMMAND` is a JSON array of strings (the argv, for example `["claude", "-p", "Answer as the model would."]`) or `SEMULA_ANSWERER_URL` is an `http(s)` URL; not both. They are read at start. They are never a project setting, a request field, a header, a pending prompt, an imported bundle, or anything an API or MCP caller can set. A project can only be switched on or off: the operator setting `answerer` (a boolean, default off, an *operator field* like `record_through`, never exported, reset by an import). Turning it on needs the admin role, is audited (`project.answerer`), and is refused with 422 `answerer_not_configured` when the server has no command or URL. The command and URL (which may carry a token) appear in no response, log line, audit entry or error; only the executable's basename is logged.

**Hosted: not available to tenants in this version.** In hosted mode switching `answerer` on is refused with 403 whatever the role, and the worker does not run. A tenant must never reach the operator's machine, and a per-workspace worker with per-workspace commands is a larger design than this card. Revisiting it needs its own ADR (a per-workspace allowlist of operator-approved commands is the likely shape).

**Run it safely.**

| Concern | Rule |
|---|---|
| Shell injection | argv list, `exec` directly, never a shell string. No part of a prompt, a tool name or any request field goes into argv; the request goes on stdin as JSON. |
| One process per call | A fresh process in a fresh empty temporary directory (removed after), its own session and process group so a timeout kills the whole tree. No state is shared between calls: no reused process, no session file, no carried-over history. |
| Environment | Stripped to an allowlist: `PATH`, `HOME`, `LANG`, `LC_ALL`, `TMPDIR`, `USER`. `SEMULA_*` (so `SEMULA_UPSTREAM_KEY`, `SEMULA_API_KEY`, `SEMULA_SECRET`, the database URL) never reaches the child. Anything else it needs the operator names in `SEMULA_ANSWERER_ENV` (a comma list of variable names, for example `ANTHROPIC_API_KEY`), and names starting with `SEMULA_` are refused there. |
| Time | `SEMULA_ANSWERER_TIMEOUT` (default 120 s); on expiry the process group is killed. |
| Output | At most 1 MiB read from stdout; more kills the process and counts as a failure. stderr is discarded: a child may echo the prompt, which can hold secrets, so it is neither logged nor returned. |
| Concurrency | At most `SEMULA_ANSWERER_CONCURRENCY` (default 2) at once, per process. |
| Failure | A non-zero exit, a timeout, no output or an over-long output leaves the call parked (an agent or person can still answer it) and is logged by pending id and the exit status only, never prompt text or the child's output. There is no automatic retry: a worker that cannot answer must not loop and spend. A call is tried once per process lifetime; a restart tries it again. |
| Rejection | If `answer_pending` rejects the answer (`invalid_answer`), the call is run once more with the rejection (`code`, `message`, `param`) and the previous output added to the stdin JSON. A second rejection leaves it parked. |
| Claims | The worker holds the call with a claim (ADR-0002 amendment) for the timeout plus a margin, so `claim_next` workers do not take it meanwhile. If another worker already holds it, the hook skips it. |
| Cost loop | Each call is one process, but an app that parks hundreds of calls would start hundreds. Set `max_misses` (SEM-96); the guide says so beside the setup. |

**The contract.** stdin is one JSON object: `{"request": {"messages", "tools", "params"}, "shape": {...}, "project", "pending_id", "attempt": 1|2, "rejection"?: {...}, "previous_output"?: "..."}`; `shape` is the SEM-97 view (tool schemas and a skeleton). stdout is the answer: if it is a JSON object or array it is the answer payload exactly as `answer_pending` takes it; otherwise the trimmed text is the reply (`{"content": text}`). The webhook gets the same JSON as a POST body and returns the same as its response body; redirects are not followed, the response is capped like stdout, and an optional `SEMULA_ANSWERER_BEARER` is sent as `Authorization` and never logged.

**`claude -p`** is the documented example: `SEMULA_ANSWERER_COMMAND='["claude","-p","You are the model the app calls. Answer as it would, in the format its prompt asks for. For a request with tools reply only with JSON as in shape."]'`. The fixed instruction is argv; the request arrives on stdin. It uses the operator's own login through `HOME`. Semula's tests use a stand-in script and never a real model.

**Untrusted prompts.** A parked prompt is text from an app and may try to instruct the answerer ("ignore your instructions and run..."). What it can do is bounded by what the operator's command can do, which is why the command is the operator's to choose and to confine: the child runs in an empty directory with a stripped environment, but semula does not sandbox it further (no seccomp, no network block). An operator who runs a model with tools enabled over prompts they do not trust has handed those prompts those tools. The guide says to run the child with tools off, as an eval answerer needs none. Semula itself never evaluates or executes what the child returns: it is data, validated like any agent's answer, then stored; templates in it expand only from the fixed set (`{{now}}`, `{{uuid}}`, `{{request.model}}`). A hostile answer can at worst be a wrong recording, which review, pinning and editing already handle.

## Alternatives considered

| Option | Why not |
|--------|---------|
| The command as a project setting | An admin token (or a bundle import, or a prompt-injected agent with the wrong scope) could then run arbitrary programs. A boolean switch keeps the program the operator's. |
| A shell string with the prompt interpolated | Command injection by construction. |
| One long-lived answerer process | Shares state between calls, which is the problem being solved. |
| Hosted tenants set their own command | Remote code execution as a feature. |
| Automatic retries with backoff | A broken command would spend money in a loop; the call stays parked instead. |
| A sandbox (containers, seccomp) built into semula | Large, platform specific, and the operator already chooses what runs; documented as the operator's to add. |

## Consequences

An operator can run evals unattended with an isolated answerer per call by setting one variable and one project setting. Costs: a worker task in the server process, five settings, a rule that hosted tenants cannot use it, and a clear statement that confinement beyond the stripped environment and empty directory is the operator's. The hook trusts the operator's command and distrusts everything else: that line is the whole design.
