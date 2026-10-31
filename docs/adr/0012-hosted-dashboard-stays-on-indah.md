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

## Amendment (2026-10-31): evaluated for the public internet (SEM-114)

ADR-0012 chose indah for a hosted dashboard that teams run for themselves. A free public instance (semula.abangai.dev, epic SEM-E20) is a harder setting, so the question was asked again with a browser and a scanner rather than by reading. **Verdict: stay on indah.** Nothing found sits in indah's renderer, and every finding was in semula's own code and is fixed.

### Threat model

What is worth taking: a person's session (the cookie), the API tokens and invitation links they create, the recorded prompts and answers of their workspace, and the other tenants' data. Who the attackers are, in order of likelihood:

1. **An app or a person who controls text semula stores.** Prompts and answers, tool descriptions, a project name, a token name, an invitation email, a display name chosen at the sign-in provider. Another person reads that text in their dashboard. This is the main risk of a multi-tenant site: stored script run in a victim's session.
2. **A person who types an address.** The path and query of `/ui`, `/auth/*`.
3. **A site that frames or links ours** (clickjacking, link injection).
4. **Someone who can read a log.** Secrets in an address.
5. **A tenant who wants another tenant's data.** Covered by ADR-0011 (Postgres row-level security, the service filter) and its own tests, not repeated here.

Trust boundaries: the browser to Caddy to semula (one proxy, believed only at its own address, SEM-111) to Postgres (a role that owns no table). The dashboard is a server-driven tree streamed over SSE; the browser code is indah's, and semula writes no JavaScript.

### What was tested

- **A real browser, three ways of running script, every screen** (`tests/browser`, `make test-browser`, a CI job; Chromium and Firefox). A hosted workspace is seeded with a payload that tries seven routes to script (a `<script>` tag, an `onerror` image, an `onload` SVG, a `javascript:` markdown link, a `javascript:` anchor, a `</script>` breakout, a quote breakout) plus an external markdown link, in every field a person or an app controls: the prompt in each message role, the tool and parameter descriptions, the model name, an entry's prompt, answer and tool-call arguments, a near miss in review, the playground, the live log, the workspace and project names, two people's names and emails, an invitation, a token name. Each of Pending, Entries (and the editor), Review, Playground, Log, Members, Tokens, Audit and Usage is opened as an admin and must **show** the payload as text, so a screen that drops it cannot pass for safe. Then the page is searched for `on*` attributes, script URLs, elements built from the text, inline script, links to other sites, a script that ran, a dialog, and any request the page policy had to block. The audit is itself tested against a page that has every one of those hazards.
- **Addresses:** a project name that is markup, in `/ui/projects/<name>/log`, `/ui/<name>`, `/ui/workspace/<name>`, and the query of `/auth/login` and the path of `/auth/invite/<token>`.
- **Framing:** another origin framing the sign-in page and the dashboard, in a real browser (and a frameable page as the control).
- **Headers and cookies** on the dashboard, the sign-in pages and the API, as integration tests, and the cookie flags in `test_sign_in.py` (existing).
- **OWASP ZAP baseline** (passive scan, `zap-baseline.py` from the stable image) against a hosted server with that seeded workspace: 0 failures, 2 warnings, 66 passes at the end. The first run had 4 warnings, one of them a 500.

### Found, and fixed

| | Finding | Fix |
|---|---|---|
| Low | A person's display name, chosen at the provider, was put into a markdown line, so `[sign in again](https://evil.example)` was a live link in their own sidebar on every screen (only they see it). Found by the foreign-link check. | Plain text. |
| Low | A malformed or unknown project in the address gave a 500 and a traceback (indah refuses the link), or an error in the log for each screen, holding the text typed, newlines and escape codes included. | A shared `spawn` that treats "no such project" as normal; an address that cannot be a project opens the default one. |
| Medium | The sign-in and invitation pages (written by semula) had no security headers: they could be framed (clickjacking the *Join* button), had no policy and could be cached. The JSON API was sniffable and cacheable. | A strict policy on those pages (no script, style by hash, `frame-ancestors 'none'`, `form-action 'self'`), `nosniff` and `no-store` on every response outside the dashboard, `Permissions-Policy`. |
| Low | The sign-in `code` and `state`, the dashboard stream's session id and an invitation token (single use, but bearer) were written to the access logs, uvicorn's and Caddy's. | A filter on uvicorn's access logger and a filter on Caddy's; the smoke test fails if a secret reaches either log. |
| Low | A provider that is down or answers rubbish gave a bare 500 (found by ZAP). | A page that says the provider could not be reached, with a 502 and the same headers. |

Not a finding but now enforced: HSTS, `Permissions-Policy`, `Cross-Origin-Opener-Policy` and `Cross-Origin-Resource-Policy` come from Caddy (`deploy/droplet`), checked by its smoke test.

### What indah sends, and what we accept

The dashboard's policy is `default-src 'none'`, scripts by nonce or same origin only, styles by nonce, `X-Frame-Options: DENY`, `nosniff`, `no-store`. Two things in it are looser than a hand-written policy would be, and neither can run script: `style-src-attr 'unsafe-inline'` (Svelte writes `style="..."` attributes; an attribute cannot run code) and the OpenStreetMap tile host in `img-src` (indah always allows it for its `Map`; semula has none). `Cross-Origin-Embedder-Policy` is not set: `require-corp` would break those tiles and nothing here needs isolation. The ZAP warning "Non-Storable Content" is `no-store`, on purpose.

### Not verified

- **WebKit** could not be started on the machine used (missing system libraries), so Safari's engine was not run. **Phone width, keyboard use and a screen reader** were not looked at. These are on a card (SEM-124).
- **Real Google and GitHub sign-in** (SEM-116). The flow is tested against a stand-in.
- **Rate limits and caps** on sign-in routes, event streams and sessions (SEM-115).
- ZAP ran **unauthenticated and passive**: it saw the sign-in pages and the redirect, not the signed-in dashboard. The browser tests cover the signed-in screens; an authenticated active scan was not run.
- This is one person's evaluation with these tools, not a penetration test. A hosted product with paying customers should have one.

### What would move the dashboard off indah

Move screen by screen to Svelte + Vite (as above) if any of these happens: a stored-text finding turns out to be in indah's renderer (not in what semula passes to it) and is not fixed within two weeks of a report; the policy cannot be tightened where a customer or an audit requires it (the style attribute and the tile host); or WebKit, phone or keyboard testing finds problems indah cannot fix. Until then the cost of a second toolchain, a second client and a second test style (ADR-0012) is higher than what was found.
