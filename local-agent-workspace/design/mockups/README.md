# Studio mock-up

The UI revamp replaced the old styles and themes with one light style. Three directions were drawn as mock-ups.
The owner chose B, "Studio", and approved the built result on 2026-11-15. The other two directions (A Paper and
C Console) are removed. `frontend/static/styles.css` is the real source of the style now. This file is kept as
design history.

`b.css` has two parts:

1. A `:root` block that sets every colour token of `frontend/static/styles.css`, the fonts, and the token
   groups `--space-1` to `--space-8` (4 px base), `--radius-s/m/l`, `--text-xs` to `--text-xl` and
   `--control-s/m`.
2. A short block of shape rules that use those tokens: radii, borders or shadows, row density, message shape,
   the composer, cards, buttons, the top bar and the dialog frame.

## Studio

A cool grey-white canvas with white cards that a soft shadow lifts. Rows, cards and messages are compact. One
indigo marks primary buttons, the selected chat, focus and the user's message bubble. Controls are rounded (8, 12
and 16 px) and the send button is round. The font is the system UI font.

## How to render it

Run this from the `local-agent-workspace` folder. It needs Playwright and its Chromium, as the UI tests do.

```
python3 scripts/ui_screenshots.py --out /tmp/ui/after
python3 scripts/ui_screenshots.py --out /tmp/ui/b --tokens design/mockups/b.css
python3 scripts/ui_screenshots.py --tokens design/mockups/b.css --contrast
```

The script mounts the whole app on the UI test harness with the fake backend and a generic demo project
(`scripts/_ui_sample_data.py`). It writes `<screen>-<width>.png` for ten screens: `empty`, `chat`, `approval`,
`composer`, `workspace`, `settings`, `agent-tools`, `context`, `workflow` and `plan`. The default widths are
1280 px and 390 px. `--contrast` prints the ratios in the table below and writes no picture.

## Contrast ratios

The rules: body text at least 4.5:1, large text and control borders at least 3:1 against their background.
`--border` is a decorative divider and is not a control border. `--border-strong` is the border of inputs and
outline buttons.

| Pair | Needs | Studio |
|---|---|---|
| ink on canvas | 4.5 | 15.97 |
| ink on surface | 4.5 | 17.41 |
| ink on sidebar | 4.5 | 15.00 |
| ink-2 on surface-2 | 4.5 | 10.09 |
| muted on canvas | 4.5 | 5.72 |
| muted on sidebar | 4.5 | 5.37 |
| muted on surface-2 | 4.5 | 5.51 |
| accent-text on surface | 4.5 | 7.90 |
| accent-text on accent-soft | 4.5 | 6.68 |
| on-accent on accent | 4.5 | 6.29 |
| on-primary on primary | 4.5 | 6.29 |
| danger on danger-soft | 4.5 | 5.75 |
| success on success-soft | 4.5 | 4.68 |
| warning on warning-soft | 4.5 | 5.23 |
| info on info-soft | 4.5 | 5.07 |
| terminal-ink on terminal-bg | 4.5 | 12.79 |
| code-keyword on surface-2 | 4.5 | 6.32 |
| code-string on surface-2 | 4.5 | 5.44 |
| border-strong on surface | 3 | 3.62 |
| border-strong on canvas | 3 | 3.32 |
| focus on canvas | 3 | 5.77 |
| focus on sidebar | 3 | 5.42 |
