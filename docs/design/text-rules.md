# Text rules for the dashboard

Every word on a screen competes with the information the user came for. These rules came from reviewing the first design, which explained itself on every panel, and from a short research pass (below). A screen is not done until each string passes them.

## The four checks

A string stays only if all four are true:

1. **The user acts on it.** It is a label for something they can do or read to decide.
2. **It is not said elsewhere on the screen.** One fact, one place. If the tier rail shows which tier answered, there is no "Matched by" column and no sentence repeating it.
3. **It is not the default state.** Show what differs: no "Replay" label (the selected segment says it), no version number until there is a conflict, no "none".
4. **It is not a footnote.** A sentence that restates the interface ("A rejection is remembered.") is deleted. One that adds a fact the user may need once moves behind an ⓘ.

## Where text goes

- **Visible, always:** labels, values, errors, and what a click will do ("Approve", "Answer").
- **Behind an ⓘ tooltip:** supplementary facts, under about 80 characters. Opens on hover **and** on keyboard focus, never hover-only, and holds nothing needed to finish a task. (Touch: the same button, tapped.)
- **In a title attribute:** keyboard shortcuts ("Approve (A)").
- **Nowhere:** marketing copy, restated state, explanations of obvious controls.
- **A disabled control** gets a tooltip saying why. That is the one place a tooltip is expected.

## Wording

Sentence case. Plain verbs. Name things by what people do, not how the system is built. The same action keeps the same name through the flow ("Answer" on the button, "Answered" in the list). Errors say what is wrong and where, in the field's own helper text ("Required"), not in a paragraph. No apologies.

## Sources

[NN/g, aesthetic and minimalist design](https://www.nngroup.com/articles/aesthetic-minimalist-design/): every extra unit of information competes with the relevant ones, and removing too much fails too. [PatternFly, embedded assistance](https://v4-archive.patternfly.org/v4/design-guidelines/usage-and-behavior/embedded-assistance): critical information stays inline, nice-to-have context goes in a tooltip or popover. [Microsoft, tooltips and infotips](https://learn.microsoft.com/en-ie/Windows/Win32/uxguide/ctrl-tooltips-and-infotips): hover-triggered, so keyboard access is needed. [Material, writing](https://material.io/design/communication/writing.html): short, scannable text covering few concepts at a time.
