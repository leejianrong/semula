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

## Behaviour to know

- **Event handlers run on the event loop.** A synchronous `on_click` that touches the database blocks every tab. We use `async def` handlers that call the service through `asyncio.to_thread`. A line in the guide would save others from finding this the hard way.
- **`POST /api/event` has no Origin check and parses JSON whatever the content type**, so a page on another site can send it (a `text/plain` body is a CORS "simple request"). An event with no `sid` goes to one shared `default` session built by the session factory. A tab's own session id is unguessable, so a stranger cannot drive someone's tab, but the shared session is reachable by anyone who can reach the port. This is indah's H2. For now semula switches the dashboard off whenever `SEMULA_API_KEY` is set, and relies on loopback otherwise.
