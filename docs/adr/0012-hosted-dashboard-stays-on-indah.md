# ADR-0012: The hosted dashboard stays on indah

- Status: Accepted (2026-10-23, owner)
- Date: 2026-10-23
- Deciders: Jian (owner)
- Builds on: [ADR-0006](0006-headless-first-dashboard-on-indah.md), [ADR-0010](0010-hosted-auth-and-tokens.md)

## Context

ADR-0006 deferred one question to the hosted build: stay on indah, or move the dashboard to Svelte + Vite + TypeScript. The hosted dashboard now exists (SEM-91), so this is decided with evidence rather than a forecast. What exists: the five local screens plus Pending (SEM-E10), and behind sign-in a per-role shell and four admin screens (Members, Tokens, Audit, Usage and deleting a workspace), all on indah 0.4.

## What was checked

**On indah, built and tested (not a prediction):**

- *Sign-in and sessions.* A page load without a sign-in is redirected; the app's own calls get 401; a session id minted for one person is refused for another (403); an event from another origin is refused. Tests: `tests/integration/test_dashboard_hosted.py` (11).
- *Roles.* A control a role may not use is not built, so it is not in the page sent to the browser (a viewer's snapshot has no Approve, Send answer or Record; only admins get the admin routes). The role is read again on every action: a demoted editor's next click fails with `insufficient_role` and changes nothing. The service enforces it either way, so a screen is never the only guard.
- *Secrets.* A new token or invitation link goes through `SecretReveal` as one patch to the one tab. It is in no snapshot and no second tab of the same person receives it (tested for the invitation link; for tokens, the secret is in exactly one message).
- *Browser.* Chromium (Playwright) opened all four admin screens under the strict CSP with nonces and the security headers: no console errors, no CSP violations, and a token created by typing and clicking showed its secret box. Screenshots were read by eye.
- *Cost.* The four admin screens are about 450 lines of Python in `dashboard/pages/workspace.py`, plus 17 integration tests that drive them over the event stream and read the effect from the database (about 30 s). They were written in one sitting by the same code path that serves the REST API, so no second validation or error-shape layer exists.

**Svelte + Vite, measured and reasoned (not built):**

- A Svelte 5 + Vite 6 page with a table, a form and a revealed secret builds to 41 kB (15.7 kB gzipped) from 29 MB of `node_modules` in 0.5 s. The build is small and fast; that was never the worry.
- What it adds is a second toolchain and a second language in a project whose image is built from Python only (slim image budget, `make image-check`), a typed client for `/api/v1`, and a second implementation of everything the screens now get for free: CSRF and origin checks on every call, the sign-in gate, the per-role omission of controls, reveal-once, the strict CSP. Each would have to be written and tested again, in the browser's terms.
- Migration cost of what is built: about 2,900 lines of dashboard Python and 116 dashboard tests (9 integration files plus unit) would be rewritten as TypeScript components and browser tests. The tests today need no browser; they would need Playwright and Chromium in CI.

## Decision

Stay on indah for the hosted dashboard. Revisit only if one of the gaps below becomes a user-visible problem that indah cannot fix, and then move screen by screen: every surface calls the one service layer (ADR-0005), so a Svelte screen can sit beside indah ones (the dashboard calls the service in-process today, and the same methods are behind `/api/v1`, which the CLI and MCP already use).

## Gaps found (also in `docs/INDAH-FINDINGS.md`)

- Row actions are addressed by position (`a0`, `a1`, ...), so a test, and anyone driving the protocol, must know the order. A name would be sturdier.
- A row action's label is the same on every row: "Make viewer" shows on a viewer.
- No form: a text box, a select and a button are three signals, so there is no "disabled until valid" or field-level error. Errors are shown in a line of plain text.
- Select options are fixed when the page is built, so the token screen's project list misses a project made after the tab opened.
- Every route's components are built for every tab, so all four admin screens are built for an admin on any page (their loads wait for the screen to be opened, so nothing is queried early).
- `ClientDisconnect` is logged as a traceback when a browser navigates away during `POST /api/session`.
- The frame is still 1080 px wide and a table of five columns is tight in it; small screens were not looked at.

## Not verified

Only Chromium on Linux, and only the admin screens by eye: no other browser, no keyboard or screen-reader pass, no phone width, and no real identity provider (sign-in is tested against a stand-in). A hosted product with many users would want those, on either stack.

## Consequences

Buys the shipped dashboard as it is and keeps one language and one test style. Costs a dependency on indah's release pace for fixes to the gaps above, and a smaller pool of people who know the framework.
