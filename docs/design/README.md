# Dashboard design

Status: proposed, second round (SEM-43 local design, SEM-44 hosted additions). Designed freely, not bound by what indah can do today (QUESTIONS.md, F2). Every screen lists what it needs from indah; nothing here has been filed on the indah board.

**Where to look.** The canvas (16 boards: 8 screens, each in light and dark): <https://claude.ai/artifact/H4Qe5PkpqF2a81d34Fpv28>. Logo options: <https://claude.ai/artifact/3t1H2GT8jybs52rULLox7G>. Sources are committed: [`artboards/`](artboards/) (one `.dc.html` per screen, thin `*Dark` wrappers, and `canvas.json` with the indah notes), [`tokens.json`](tokens.json), [`logo/`](logo/), and the [text rules](text-rules.md). The content is the real support-ticket run from [`../E2E.md`](../E2E.md).

## The idea

The dashboard is used next to a terminal while tests or an agent record and replay LLM calls. Its one job: **what just happened to that call, and what needs me?**

**The tier rail is the signature.** Every call is matched by a cascade of five tiers: exact, normalised, alias, words, meaning. The rail draws it as five stations on a line: unmatched ones the call passed through are hollow, the one that answered is filled (with its score beside it when it is not an exact match), untried ones are dashed, and a dotted one is switched off. It is the real matching logic made visible, and it replaces what used to be a "Matched by" column and a paragraph per tier.

**Red means "needs you".** One red, taken from the logo's dot, appears only for things that are parked, held or recording: the Pending badge, the Record segment, the held-call timer, the Parked number.

**The mark.** Option D, "again from the start": a loop that returns to where it began, starting at the record dot. The dot is the same `record` colour as the red in the UI, in both themes. Files in [`logo/`](logo/).

## Foundations: Material 3 tokens

Tokens are M3 roles (`--md-sys-color-<role>`), generated from the seed `#0E7C66` (the ink-green of the mark) with Google's HCT colour science, light and dark, so every role pairs with its `on-` colour by construction. [`tokens.json`](tokens.json) holds both sets. No hand-picked hex values in the screens except through a role.

- **Custom roles** (M3 has none for these), each a tonal palette so light and dark pair: `record` (hue 26, chroma 84: the logo red, `#C8271F` light, `#FF7767` dark), `warn` (amber: near matches, misses), `info` (blue: aliases, recorded). Each has `on-`, `-container` and `on-…-container`.
- **Surfaces.** Page `surface`; navigation `surface-container`; panels `surface-container-lowest` in light and `surface-container-low` in dark (the lowest tier is pure black in dark, so panels step up instead); insets (code, request text) one tier above the panel.
- **Type (M3 scale roles).** Brand slot (headline, title-large): Bricolage Grotesque. Plain slot (title, body, label): Hanken Grotesk. Data (ids, times, JSON): IBM Plex Mono.
- **Shape.** Buttons, segmented buttons and the nav indicator are full; chips and status tags 8 px; cards 12 px; fields 4 px; the sign-in card 28 px.
- **Components.** Standard navigation drawer, segmented button (mode, answer choice), filter chips, filled, tonal, outlined and text buttons, outlined text fields with supporting text, search bar, switch, secondary tabs, plain tooltip, menu. No shadow except menus; hierarchy comes from tonal surfaces.
- **State layers.** Hover 0.08 and focus 0.12 overlays on every interactive element.
- **Dark theme.** Every screen takes a `theme` tweak (light or dark). The dark boards import the light screen with `theme="dark"`, so there is one source per screen.

Material 3 itself is not imported: the markup is hand-written against the tokens, so it carries over to indah or to Svelte unchanged in meaning.

## Screens and what each needs from indah

Enhancement ids are from [`../INDAH-REQUESTS-PROMPT.md`](../INDAH-REQUESTS-PROMPT.md).

| Screen | Needs from indah |
|---|---|
| Live log | E1 push (rows within 1 s), E2 table v2 (append-only stream, row renderers), E6 resizable master-detail, E3 collapsible JSON viewer, a slot for the custom tier rail (E8 theme tokens) |
| Pending | E1 push (new parked call, rail count), E3 multiline and JSON editor, E7 dynamic rows (tool arguments, chunks), E5 toast |
| Entries | E2 table v2 (ids not indexes, server paging and filter, multi-select), E4 deep links, E5 badges and switch |
| Entry editor | E3 code editor with JSON modes and a diff, E5 confirm dialog and toast, E7 dynamic answer list, E4 deep link |
| Review | E3 side-by-side word diff (the hardest ask), E5 keyboard shortcuts, E2 live count, E6 panes |
| Playground | E7 dynamic message stack and tool rows, E3 JSON viewer, E5 switch and tooltip |
| Hosted: tokens, members, sign-in, workspaces | H1 identity, H2 login with CSRF and Origin checks, H3 roles, H6 token UI, H7 base path |

Tooltips that open on keyboard focus (E5) are required by the [text rules](text-rules.md), not optional. How well indah answers the H items feeds the decision to stay on indah or move to Svelte + Vite + TypeScript before the hosted version (QUESTIONS.md, F2). Hosted-only parts carry a dashed outline on the boards. Settings (thresholds, scrub and redaction patterns) is in the navigation but not drawn; it is a plain form over `PATCH /api/v1/projects/<id>`.

## What the design needs from semula itself

Drawing real data exposed gaps in the headless core. These are semula work, not indah work, and none was built when this was written; struck through as they land:

1. ~~**A prompt preview on each call-log row.**~~ Done (SEM-63). Each row, event, `GET /api/v1/log`, `semula log`/`tail` and MCP `list_calls` carry `preview`: the last message with text, redacted, whitespace collapsed, at most 120 characters (null on rows from before).
2. ~~**Log statistics.**~~ Done (SEM-65). `GET /api/v1/log/stats?project=&window=30m` (also `semula log --stats` and MCP `log_stats`): one bucket per minute for the last N minutes (the current one included; `hits`, `misses`, `recorded`, `timed_out`), `totals`, `hit_rate` (hits over every call), `median_latency_ms` (hits and misses only: a recorded or timed-out call waited for an agent) and `parked` (open requests now, whatever the window). Windows run from `1m` to `24h`.
3. ~~**Per-tier verdicts in `test_match`.**~~ Done (SEM-62). `test_match` returns `tiers`: one entry each for exact, normalised, alias, words and meaning, with a `verdict` (`no`, `match`, `candidate`, `skipped`, `off`), an optional `score` and a short `reason`. The trace is collected only when `test_match` asks.
4. ~~**Entry history.**~~ Done (SEM-66). `GET /api/v1/entries/<id>/history` (`?version=N` for one revision with its snapshot), `semula entry history`, MCP `entry_history`: version, time, actor, a short summary, `forced`, and the responses, selection and pin as the write left them; the latest 50 per entry (table `entry_revision`, ADR-0005 amendment). A 409 `version_conflict` still returns `current`; Compare reads the history.
5. ~~**Entries list filters and flags.**~~ Done (SEM-64). `GET /api/v1/projects/<id>/entries` takes `pinned`, `has_alias`, `tool_calls`, `never_hit` (bool, AND-combined, `false` selects the others) and `sort` (`hits`, `updated`, `created`) with `order`; each row has `alias_count` and `has_tools`; `total` counts the filtered set. Checked on 10,000 entries: at most 32 ms on SQLite and 55 ms on Postgres (the slowest is the existing text search); one index added, `alias(entry_id)`.
6. **Hosted: mode per project, tokens, members and roles.** Designed in [ADR-0010](../adr/0010-hosted-auth-and-tokens.md) (tokens, scopes, roles, sign-in) and [ADR-0011](../adr/0011-workspace-tenancy-and-per-project-mode.md) (workspaces, quotas, mode per project); proposed, not built.

## Decisions taken in review

- Light and dark both ship; M3 tokens, with the red kept as the accent.
- The activity strip and its three numbers stay. "Parked" duplicates the Pending badge, and is kept deliberately.
- Approving carries the old answer with it, so the review screen shows that answer with a short "Check it fits." beside it. A stronger stop (open the entry first) is still an open question.
- Not drawn: Settings, the narrow (phone) layouts beyond what the fluid root gives, and a meaning-tier colour on the rail (embeddings are off in the drawn runs).
