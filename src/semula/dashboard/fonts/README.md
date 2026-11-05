# Fonts

`HankenGrotesk-latin-wght.woff2` is Hanken Grotesk, the design's body face (docs/design/README.md), as a variable font (weights 100 to 900), Latin subset, normal style only.

- Source: the `@fontsource-variable/hanken-grotesk` npm package 5.3.0 (`files/hanken-grotesk-latin-wght-normal.woff2`), which repackages Google Fonts' Hanken Grotesk unchanged.
- Licence: SIL Open Font License 1.1 (OFL-1.1), in `OFL.txt` beside the file. Copyright 2021 The Hanken Grotesk Project Authors.
- Use: `theme.py` hands it to indah's `Theme(fonts=[...])`, which serves it from the dashboard's own origin. Text outside Latin falls back to the system font in the stack.

The other two faces, Bricolage Grotesque (display) and IBM Plex Mono (data), already ship inside indah's page, so they are not repeated here.
