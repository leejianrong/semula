# Review of the screens the first pass could not see (SEM-138)

Driven in a real Chromium against a hosted-mode instance (workspace `acme`, an admin, an editor, an open invitation, a token, three entries, one parked call), at 1440x900 and 390x844, light and dark. The seeding script is a copy of the one in `tests/browser/test_dashboard_use.py`'s `site` fixture plus a device code. "Fixed" means this PR; the rest is a list for the next cards.

## Fixed in this PR

1. **Usage: Delete workspace was the screen's one filled green button**, the same colour as every safe action, and it was live before the id was typed. It is now a ghost button, disabled until the typed id matches. The service still refuses a wrong id (`confirmation_mismatch`).
2. **No-workspace and suspended pages had no way out.** A person who belongs nowhere (signed in with the wrong account, say) could not sign out. Both pages now have a Sign out button that ends the sign-in.

## Fix next

| # | Where | What is wrong | Fix | Owner |
|---|---|---|---|---|
| 3 | Members, Tokens, Audit, Usage | The top bar still shows the project switcher and the Replay or Record control. They do nothing on a workspace screen and the mode is easy to change by accident. | Build the bar per route: project and mode only under `/projects/...`. Needs a route-aware `top` in AppShell, or the bar built inside each project page. | semula or indah |
| 4 | Every form (members, tokens, new entry, entry editor, review) | Errors are plain body text: `invalid_email: That is not an email address. (at email)` under the heading, `Write the prompt the app will send.` below the buttons of the New entry dialog. No error colour, the code leaks into the sentence, and the message is far from the field. The text rules ask for the field's own helper text ("Required"). | Show `message` only (keep `code` for logs) through indah's own `error=` on `TextInput`, `Select` and the editors, which draws it under the field in the `err` colour and marks the field invalid (already in 0.11, ADR-0047; semula does not use it yet). Errors that belong to no field need a toned `Text`. | semula |
| 5 | Every table | indah prints the table's `label` as a visible caption above it ("Members" under "Members", "History" inside the History card, "Audit log" under "Audit"). Text rules 1 and 2. | indah: make the caption optional (`label` for assistive tech only). Ask in `INDAH-REQUESTS-PROMPT.md`. | indah |
| 6 | Entry editor | Card "Edit the answer" holds a tab-like header "Edit answer". "Request" shows the role "user" as a tiny caption. The hint "Plain text is the reply. Start with { for a tool call..." is always on. | Drop the inner header; move the hint behind an ⓘ (text rules). | semula |
| 7 | Audit | When is cut to "2026-11-05 02:..." at 1440 px, Target is a raw ULID, Detail is raw JSON, and the Action filter spans the page. | Width for When (or relative time as in the log, with the exact time on hover), a short target ("project shop"), detail as `key value` text, filter at a field's width. | semula |
| 8 | Usage, Members, Tokens | Numbers and dates are left aligned in a mono column (`567 B`, `no limit`); the used and limit columns do not line up. Row actions (Make viewer, Make editor, Make admin, Remove) are four buttons per row. | Right-align numbers; one menu per row, or a role select. | semula, indah (Col align) |
| 9 | Sidebar account block | Name, "admin in acme" and an outlined Sign out button sit above the navigation and compete with it; Sign out is the largest control in the rail. | One quiet row (name, role in workspace), Sign out as a text link at the bottom of the rail. | semula |
| 10 | No-workspace page | An indah page with a floating "live" dot, no card, and a different look from the sign-in, sign-up and device pages (Lane A's one styled page). | Serve it from `public_page.py` like the others, or give it the same card. | semula |
| 11 | Sign-up, device approval | Footer shows "Sign in" to someone who is already signed in. | Show "Sign out" when a session exists. | semula |
| 12 | Tokens after creating | The Name field keeps "ci runner" after the token is made, so pressing Create again makes a second one with the same name. | Clear the name on success. | semula |
| 13 | Hover and selected rows | Unconfirmed: the hovered row (the second, which is also striped) could not be told from striping in the screenshot, so retest on an odd row. A row in entries navigates at once, so there is no selected state to see. | Retest; if confirmed, an indah theme token for row hover. | indah |
| 14 | Toasts | No screen uses a toast. Token creation, invitation sent, workspace deleted all report as a line of plain text that can scroll out of sight on a phone. | indah `ctx.toast` (E5 in `INDAH-REQUESTS-2.md`) then use it for these confirmations. | indah |

## Phone (belongs to SEM-136)

At 390 px the Tokens form squeezes Name, Scope and Limit to project into three 55 px fields beside the button; the tokens table shows four of seven columns and clips Rotate and Revoke; the mode control touches the right edge (Record is cut off at 383 px); Members and Audit tables clip their action columns. The navigation is already a drawer behind the menu button.

## Looked fine

Sign-in, sign-up and device approval pages (Lane A) at both sizes and themes; the token secret panel (accent edge, Copy and I have saved it, one filled button); the New entry dialog (focus is trapped, Cancel is ghost); dark theme on Members (the one dark screen read closely; the other dark shots were taken but not inspected).
