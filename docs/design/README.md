# Dashboard design

Status: proposed, for review (SEM-43 local design, SEM-44 hosted additions). The design was made freely, not bound by what indah can do today (QUESTIONS.md, F2). Every screen lists what it needs from indah; nothing here has been filed on the indah board.

**Where to look.** The design canvas (eight artboards, open it in the Claude artifact viewer): <https://claude.ai/artifact/H4Qe5PkpqF2a81d34Fpv28>. Its source is committed in [`artboards/`](artboards/): one `.dc.html` per screen plus `canvas.json` (layout and the sticky notes with the indah needs). The content on every screen is the real support-ticket run from [`../E2E.md`](../E2E.md), not placeholder text.

## The idea

semula's dashboard is used next to a terminal while tests or an agent record and replay LLM calls. Its job is to answer one question quickly: **what just happened to that call, and what needs me?**

**The tier rail is the signature.** Every call is matched by a cascade of five tiers: exact, normalised, alias, words, meaning. The rail draws it as five stations on a line. Stations the call passed through unmatched are hollow, the station that answered is filled (with the score beside it), and stations never tried are faint and dashed. A dotted station means that tier is switched off (embeddings). It appears small in log rows, as a written explanation in the log detail pane, and as a vertical walkthrough in the playground. It is the real matching logic made visible, so it teaches the product and it explains every hit and miss without a manual.

**The mode lamp.** Replay or Record is a hardware-style control at the bottom of the rail, with a green or red lamp. Red appears nowhere else except for things that are parked or recording, so red always means "something is waiting on you".

**Layout.** A dark rail (navigation, project, mode) beside a pale working area. Most screens are a list beside a detail, so what you learn on one screen carries to the next.

### Tokens

| Role | Value |
|---|---|
| Rail and ink | `#14201B` |
| Working area | `#F1F3EF` (panels `#FFFFFF`, lines `#D8DDD5`) |
| Muted text | `#52625A` (4.5:1 or better on white and on the working area) |
| Hit | `#0E7C66` on `#DDF1E9`, text `#0B6B55` |
| Miss and near match | `#F4B740`, text `#7A4B00` on `#FBEBC8` |
| Recorded, alias | `#2848C9` on `#E1E8FB` |
| Recording, parked, held | `#C8281F` (the only red; lamp `#FF5A52`) |
| Hosted only | `#6A42C2` dashed outline with a "New in hosted" label |
| Display type | Bricolage Grotesque 600/700: screen titles, the wordmark, large numbers |
| UI type | Hanken Grotesk 400/500/600, 14 px |
| Data type | IBM Plex Mono 400/500, 12 px: ids, times, JSON, prompts |

Two choices to name: the working area is cool pale grey-green rather than cream, and the accent is a single recording red tied to what the product does, rather than a bright colour on near-black. Copy is plain and sentence case, and names things by what people do ("Answer and release the call", "Approve: serve this prompt from that entry").

## Screens and what each needs from indah

Enhancement ids are from [`../INDAH-REQUESTS-PROMPT.md`](../INDAH-REQUESTS-PROMPT.md).

| Screen | What it shows | Needs from indah |
|---|---|---|
| Live log (`Main`) | 30-minute activity strip, call list with the tier rail, a detail pane that explains each tier's verdict and shows the redacted request | E1 push (rows within 1 s), E2 table v2 (append-only streaming rows, cell renderers), E6 resizable master-detail, E3 collapsible JSON viewer, a custom rail component (E8 theme tokens plus a slot) |
| Pending queue (`Pending`) | Held calls with a countdown to the timeout, the full request, an answer editor with Text, Tool call and Stream chunks tabs, inline validation | E1 push (new parked call, rail count), E3 multiline and JSON editor, E7 dynamic rows (tool arguments, chunks), E5 toast |
| Entries (`Entries`) | Filter chips, search, multi-select with bulk pin and delete, paging, export for git | E2 table v2 (ids not indexes, server paging and filter, multi-select), E4 deep links, E5 badges and switch |
| Entry editor (`Entry`) | Request, answers with a selection policy, pin, version history, a version-conflict banner | E3 code editor with JSON modes and a diff, E5 confirm dialog and toast, E7 dynamic answer list, E4 deep link |
| Review queue (`Review`) | Candidates by score, word diff of the new prompt against the entry, the answer that would be served, approve or reject | E3 side-by-side word diff (the hardest ask), E5 keyboard shortcuts, E2 live count, E6 panes |
| Playground (`Playground`) | A request composer and a tier-by-tier explanation of what would happen, with the response preview | E7 dynamic message stack and tool rows, E3 JSON viewer, E5 switch and tooltip |
| Hosted: tokens and members (`HostedTokens`) | Workspace switcher, scoped API tokens (shown once), usage snippets, members and roles; mode per project | H1 identity, H3 roles, H6 token UI |
| Hosted: sign in (`HostedSignIn`) | OIDC sign-in, no open sign-up, workspace switcher with roles and invitations | H1, H2 login with CSRF and Origin checks, H3, H7 base path |

How well indah answers the H items feeds the decision to stay on indah or move to Svelte + Vite + TypeScript before the hosted version (QUESTIONS.md, F2). Settings (thresholds, scrub and redaction patterns) is in the navigation but not drawn yet; it is a plain form over `PATCH /api/v1/projects/<id>`.

## What the design needs from semula itself

Drawing real data against the current API exposed gaps in the headless core. These are semula work, not indah work, and none is built yet:

1. **A prompt preview on each call-log row.** The log lists calls but carries no text, so the live log cannot show which prompt it was. Add a short redacted last-message preview to the row and to the event. (Redaction rules already exist.)
2. **Log statistics.** The activity strip and the hit rate, median latency and parked count need `GET /api/v1/log/stats` (per-minute buckets for a window, plus the three numbers).
3. **Per-tier verdicts in `test_match`.** The playground and the log detail explain each tier in words. `test_match` today returns the winning tier and candidates only; it should return a verdict and a reason for every tier, including "off" and "not needed".
4. **Entry history.** The editor lists versions (who, when, what changed). Today an entry only has a version number. Keep a small change history per entry, and have the conflict banner use it.
5. **Entries list filters and flags.** Filters for pinned, has an alias, several answers, tool calls and never hit; an alias count and a tool-call flag on each row; sort by hits and by updated.
6. **Hosted: mode per project, tokens, members and roles.** Mode is per process today. These belong to SEM-E11 (ADRs for tokens and tenancy).

## Findings and decisions to confirm

- **Approving carries the answer with it.** The review screen shows the answer that will be served and warns that later turns of a conversation may not fit it (the end-to-end run served "cracked screen" text for a "broken screen" ticket). Do you want a stronger stop, for example requiring the reviewer to open the entry first?
- **Meaning candidates** are labelled "meaning" in the review list so a reviewer reads them more carefully than a word match. A distinct colour for a lit meaning station is a proposal (violet is defined in the review styles but not used on any drawn screen, because embeddings are off in the drawn runs).
- **Dark mode** is not drawn. The tokens are named so it can follow.
