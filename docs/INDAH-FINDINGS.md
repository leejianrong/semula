# What building on indah 0.3 turned up

A running list from building the dashboard (SEM-E10) on indah 0.3.0. Each item says what we hit, what we did about it and what would help. Nothing here is filed on the indah board; the owner decides what to pass on. Newest at the bottom of each section.

## Packaging and start-up

- **No `py.typed`.** Strict mypy rejects `import indah`. We ignore the module's types (`pyproject.toml`). A marker file, and type hints that hold up under strict mode, would let us check our own dashboard code against it.
- **Import and build cost.** `import indah` takes about 0.3 to 0.4 s (pydantic models in `protocol` and `components`). semula's container cold start is budgeted at 2 s and measures 1.94 s on CI, so the dashboard is built on the first request to `/ui`, never at start-up. A lazier import would let us build it earlier.
- **Fonts and offline.** Bundled (Bricolage Grotesque, IBM Plex Sans and Mono as base64), nothing fetched at run time. Good for a local tool. The design's body font (Hanken Grotesk) is not available, so body text is IBM Plex Sans.

## Look and feel

- **`<title>` is always "<title> - indah".** `title=` renames the tab but the suffix stays; `brandName` exists in the page chrome but `create_app` has no argument for it.
- **Theme tokens are a fixed list** (`bg`, `surface`, `surface-2/3`, `text`, `muted`, `outline`, primary and secondary pairs, `ok`, `warn`, `err`). The design has custom roles (`record`, `info`, each with an `on-` and container) and the Material 3 surface tiers. We map `record` red to `err` and cannot give `info` its own blue: the `info` badge tone follows the primary container.
- **Table time columns are relative only** ("3 s ago", absolute in the tooltip). The log wants `09:14:22.318`, so we send a preformatted string in a `mono` column and lose sorting by time on that column (the table keeps arrival order).

## Controls and layout

- **The page frame is fixed at 1080 px** (`.shell { max-width: 1080px }`). A left rail plus a table with five columns is cramped in it; a fluid option (`create_app(layout="fluid")`, or a token) would fix it.
- **No chip or segmented control.** `Radio` stacks its options vertically, `Tabs` wraps children, and `Button(variant=)` cannot change with state. The log's filter is a `Select` and the mode is a `Switch`. The design draws a segmented Replay/Record control and filter chips.
- **`Select` options are fixed when the page is built**, so a project created later does not appear in the switcher until the tab reloads. A signal for the options would let it follow.
- **`Link` takes a plain string label**, so a live count (the Pending badge in the design) cannot sit inside a nav link; it would need a `Link` and a `Badge` side by side, and a badge cannot hide itself at zero.
- **A plain `Text` renders heading-sized** (only `markdown=True` gives body size). Easy to trip over; the guide could say so.
- **One logo file for both schemes.** The dark mark vanishes on the dark page. We use an SVG with its own `prefers-color-scheme` rule, which follows the operating system rather than indah's toggle.
- **The first paint shows placeholders.** The session factory is synchronous, so stats and counts load a moment later and arrive as patches (a dash, then the number). An async factory, or an initial-data hook, would avoid the flicker.
- **Per-tab work is hand-rolled.** A refresh loop is an `asyncio` task cancelled from `ctx.on_close`; it works well, and a helper for it would save everyone the same ten lines.

- **The Pending screen (SEM-48), what it took.** A parked-call count in the nav is a `Link` and a `Badge` side by side (a `Link` label is a plain string), and the badge shows `0` instead of hiding. `Select` and `Table` have no way to say "this one" in tests except `label`, so every table needs one. A `TextArea` sends `input` events (no submit), so the Send button is the only way to submit and Enter inserts a newline. The form cannot be hidden until a call is picked (no conditional render), so it shows a prompt to select one. (Superseded: the Pending screen now uses indah 0.4's `CodeEditor`.)
- **`ctx.latest` drops repeats.** A signal ignores a value equal to the one it holds, so a topic whose events are identical ("queues moved" for the same project) wakes the first time only. `ctx.tail(topic).listen(...)` sees every event; we use it for the service's events. Worth a line in the guide.
- **Every route's components are built for every tab** (both tables are in the first paint), so a screen's effects run even when it is not shown.

## What worked first time

`Topic` and `ctx.tail` (a call reaches every open tab in a few milliseconds, one small `append` patch each), `Table(follow=True)`, `Router` with deep links, the bundled fonts, `List` with a render spec and per-row `style` (the tier rail), and the dark theme.

## Behaviour to know

- **Event handlers run on the event loop.** A synchronous `on_click` that touches the database blocks every tab. We use `async def` handlers that call the service through `asyncio.to_thread`. A line in the guide would save others from finding this the hard way.
- **`POST /api/event` has no Origin check and parses JSON whatever the content type**, so a page on another site can send it (a `text/plain` body is a CORS "simple request"). An event with no `sid` goes to one shared `default` session built by the session factory. A tab's own session id is unguessable, so a stranger cannot drive someone's tab, but the shared session is reachable by anyone who can reach the port. This is indah's H2. For now semula switches the dashboard off whenever `SEMULA_API_KEY` is set, and relies on loopback otherwise.

## The hosted dashboard on indah 0.4 (SEM-91, SEM-55)

Checked while building the sign-in gate, the per-role shell and the four admin screens, and in Chromium under the strict CSP (no console errors, no violations). Outcome and the decision are in ADR-0012.

**What worked first time:** `auth` with `Policy` and `ctx.can` (a control a role may not use is simply not built, so it is not in the snapshot); `ctx.sign_out()`; the origin and session-token checks (a session id minted for one person is refused for another); `secure`, `csp="strict"` and `security_headers`; `SecretReveal` (the secret is one patch to one tab, in no snapshot and no second tab); `Button(confirm=)` and `RowAction(confirm=)`.

**Gaps:**

- **`RowAction`s are addressed by position** (`a0`, `a1`, ... in the order given). A test or a protocol client has to know the order; a name would survive reordering.
- **A row action's label is the same on every row.** The Members table offers "Make viewer" on a viewer; it cannot be hidden or relabelled per row.
- **No form.** Three signals and a button have no "invalid" or "disabled until filled" state and no per-field error, so the screens show the service's refusal in a line of plain text (`code: message`).
- **`find` by type finds every route's component.** Every route is built for every tab, so a test looking for "the" `SecretReveal` gets the first one on any page; tests must name it by `label`. (A restatement of the existing note on routes, now with a consequence for tests.)
- **`Select` options are fixed at build**, so the token screen's project list misses a project created after the tab opened (same as the project switcher).
- **A `ClientDisconnect` traceback is logged** by `POST /api/session` when the browser navigates away while it is in flight. Harmless, but noisy in a server log.
- **An ephemeral value is sent as an ordinary patch** (`props.value` of the node), so a test, or a person reading the wire, sees it; that is expected ("to the open tab only") and worth saying in the docs, since "never in a snapshot" is not "never on the wire".
- **A plain `Text` is still heading-sized** and `size="body"` is needed on every sentence (see the 0.3 list).
