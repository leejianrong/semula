# Prompt for the indah coding agent: plan a round of enhancements for semula

Paste everything below the line into the coding agent working in `/home/jian/projects/abang-ai/indah`.

---

You are the coding agent for **indah**. I want you to run **one round of planning** (not implementation) for a set of enhancements requested by a sibling project, **semula**. Read this whole message, then follow "What I want back".

## Ground rules for this round

- **Plan only.** Write no framework code. You may read any file in indah.
- **Do not create or move cards on the IND Pandan board** until I confirm your plan. Draft the cards in your plan document instead.
- **Treat indah's existing "no" decisions as open for this round.** For example ADR-0008 says indah ships no auth, and ADR-0010 ships only an in-memory store. Where a request below conflicts with an ADR, do not drop the request. Say which ADR it conflicts with, and propose a superseding ADR or an amendment. Keep indah's identity otherwise: single port, no Node at install or runtime, SSE plus POST, pre-built Svelte shell, public versioned protocol.
- Use your normal planning process (for example the `plan-new-project` skill, in this repo's `docs/PLAN.md`, `docs/SLICES.md`, `docs/QUESTIONS.md`, `docs/adr/` style). One interview round at most. Ask me only about decisions where a wrong default is expensive.

## Context: what semula is

semula is a local, containerised fake-LLM server. An app points its OpenAI-compatible base URL at it. On a cache hit it replays a stored response for free. On a miss in record mode it parks the call, a coding agent (Claude Code or Codex over MCP/CLI) writes the answer, and semula saves it. Matching goes exact, then normalised hash, then BM25 (SQLite FTS5), then optionally embeddings, with weak matches going to a human review queue.

semula is built headless first (REST, CLI, MCP). The **dashboard** comes afterwards and is the part indah would provide. For the MVP the dashboard runs in the same container at `/ui`, with indah mounted as an ASGI sub-app on semula's single port. Later there will be a hosted version with login, workspaces and API tokens. **For the hosted version we have not decided whether to stay on indah or move to Svelte + Vite + TypeScript.** How well indah answers the H items below will feed that decision, so please plan them as a staged roadmap with honest effort and risk, not as a commitment.

semula and indah are both mine. The goal is that semula's needs make indah better for everyone.

Semula's design is being done freely, not bound by indah's limits. The design will be saved in `semula/docs/design/` (not written yet). Treat the screens below as the spec for now.

## Dashboard screens

| Screen | What the user does | Data shape |
|---|---|---|
| Live request log | Watches calls arrive: hit, miss, tier, score, latency. Opens one to see the request and response. | Append-only stream. Retention is 50k rows or 30 days. New rows should appear within 1 s. |
| Pending queue (record mode) | Sees parked calls, writes or edits a response, submits it, which releases the held HTTP call. | Small list, changes live. |
| Entries browser | Switches project, filters and sorts recorded entries, sees hit counts and flags (pinned, fuzzy). | Thousands of rows per project. Rows have ULIDs. |
| Entry editor | Edits a stored request and response (JSON and text), toggles pin, handles a version conflict. | Large JSON bodies. |
| Review queue | Compares an incoming request against a candidate entry, with the score, then approves or rejects. | Side-by-side diff. |
| Playground | Pastes a prompt, sees which tier and entry would fire and why. | Request and response, streaming already supported. |

## The 15 requested enhancements

Sizes are my rough estimates (S about a day, M a few days, L a week or more). Please give your own.

### Local MVP (E1 to E8)

| ID | Name | Size | What | Unlocks | Board |
|---|---|---|---|---|---|
| E1 | Server push channel | S/M | An app-level topic that any background task can publish to and any session can subscribe to, so a signal updates in every open tab. Keeps per-tab isolation. Today `Hub` is per session and nothing outside a session can push in. | Live log, pending queue badge, review queue | new |
| E2 | Table v2 | M | Rows keyed by ID (today `selected` is a row index), filter and search, server-side paging with virtual scroll, append-only streaming rows, cell renderers (badge, mono snippet, relative time), row actions, multi-select, column toggle. | Every list in the dashboard | extends IND-68 (done). Its title mentions filter; I found no filter prop on `Table`. |
| E3 | Editor and viewer family | M/L | Multiline text area (`TextInput` is single-line), bundled code editor with JSON and YAML modes, collapsible JSON tree, side-by-side diff. | Entry editor, request inspector, review diff | new |
| E4 | Routing and deep links | M | URL maps to state, sidebar navigation, working back button, links like `/projects/auth/entries/01J…` open that entry. | Project switcher, shareable links | IND-75 (todo) |
| E5 | Overlays and small controls | M | Modal, drawer, confirm dialog, toast API callable from handlers, badge, switch, tooltip, keyboard shortcuts. | Delete confirm, approve and reject, pin toggle, status chips | new |
| E6 | Resizable panes and density | S/M | Split panes with a draggable divider, master-detail layout, sticky headers, compact density mode. | Log list with detail pane | new |
| E7 | Dynamic children | S/M | A node's child list can grow, shrink and reorder at runtime. | Playground message stack, tool-call editor rows | IND-29 (todo) |
| E8 | App-level theme tokens | S | Override the design tokens for one app, plus custom logo and title, so semula's look is not locked to the default theme. | Lets semula's free-form design land as drawn | new (theme switcher is deferred in ADR-0014) |

### Hosted (H1 to H7)

| ID | Name | Size | What | Unlocks | Board |
|---|---|---|---|---|---|
| H1 | Identity in context | M | `session_factory(request)` or a `ctx.user`. The session id is server-signed and bound to the authenticated principal. Today `session_factory` takes no arguments and the browser picks the session id. | Every hosted screen. Stops one user opening another's session. | new |
| H2 | Login and request protection | L | OIDC login, cookie sessions, CSRF and Origin checks on the event and upload routes, sign-out. | Hosted entry point | new |
| H3 | Authorization | M | Roles per workspace, hide or disable components by permission, guard handlers. | Viewer, editor and admin roles | new |
| H4 | External session store | M/L | A Redis-style backend for the existing store seam. SSE resume works across replicas. | More than one replica, restarts that keep tabs | seam from ADR-0010 |
| H5 | Hardening in core | M | Move per-IP rate limits, connection caps and a tested CSP from the gallery app into indah itself. | Public hosting | extends IND-E8 |
| H6 | Secret and token UI | S/M | Reveal-once secret field with copy button, token list with revoke and last-used. | API token management | new |
| H7 | Base path and proxy support | S | Run under a path prefix behind a reverse proxy, correct client IP, an audit-log hook on events. | Gateway deployment | new |

### Which screen needs which enhancement

| Screen | E1 | E2 | E3 | E4 | E5 | E6 | E7 | E8 |
|---|---|---|---|---|---|---|---|---|
| Live request log | x | x | x |  |  | x |  |  |
| Pending queue | x | x | x |  | x |  |  |  |
| Entries browser |  | x |  | x |  | x |  |  |
| Entry editor |  |  | x | x | x | x | x |  |
| Review queue | x | x | x |  | x |  |  |  |
| Playground |  |  | x | x |  |  | x |  |
| Whole-app look |  |  |  |  |  |  |  | x |

## Priorities and constraints from semula

- **Order I expect:** E1 and E2 first, because the live log and pending queue need both. Then E3 and E4. Then E5 to E8. The H items come after, and only partly, depending on the stay-or-leave decision. Challenge this order if you see a better one.
- semula's headless core is built first, so the dashboard is not blocked on you for the first slices. Expect your first usable drop to be wanted in roughly the third of semula's slices (`docs/SLICES.md` in the semula repo, epics SEM-E2 to SEM-E7).
- **Mounting:** indah must run as a sub-app inside semula's Starlette/FastAPI app on one port, under `/ui`, sharing the process with the `/v1`, `/api/v1` and `/mcp` routes. Check that this works with the shell's absolute paths and with E4 routing, and say what changes if not.
- **Live data volume:** the log tail can reach tens of events per second during a test run. Say how E1 and E2 should coalesce so the existing frame padding and proxy behaviour do not hurt (see your own `Q-frame-coalesce`).
- **Python only on the server.** The shell stays pre-built. If E3 needs a bundled editor (for example CodeMirror), say which, its size and licence, and what it does to the wheel size.
- **Compatibility:** any wire change must be handled through your protocol versioning rules. Say when `protocol_version` would bump.

## What I want back

Write one document, `docs/SEMULA-REQUESTS-PLAN.md` in the indah repo, with these sections. Keep it tight.

1. **Readiness verdict.** For each of E1 to E8 and H1 to H7: already possible, possible in userland today with a workaround (give it), or needs framework work. Back each verdict with a file or ADR reference, and correct anything above that is wrong.
2. **Per-item design sketch.** The public Python API you would add (signatures and a five-line usage example), the protocol or shell change, and what you would reuse. For E1, H1 and H2 also say how it fits the session-store seam.
3. **Slices.** Vertical, each ending in something demonstrable. Group the items sensibly (for example E1+E2 as one slice, because the live log demo needs both). For each slice: points, build plan, tiered test plan, and the demo. Include a small **semula-shaped demo** per slice so I can see it working, for example a fake live log fed by a background task.
4. **ADRs.** Which existing ADRs conflict, which new ones are needed, and which you would draft (titles only is fine for now).
5. **Order and dependencies.** A proposed sequence, what can run in parallel, and what is on the critical path for semula's dashboard.
6. **Risks and open questions.** Only those that need my decision. Lead each with a recommendation. At most eight.
7. **Draft cards.** Epics and stories in Pandan form (title, points, Why/Do/Done when/Not in scope), ready to file on board IND after I confirm. Do not file them.
8. **Hosted take.** Half a page: after this roadmap, where would indah still be weaker than Svelte + Vite + TypeScript for a hosted product with login, workspaces and tokens, and where stronger. Be candid. I will use this to decide.

When the document is written, stop and tell me what to review. Do not start building.
