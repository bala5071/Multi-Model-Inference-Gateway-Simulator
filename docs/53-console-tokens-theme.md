# 53. The console's design tokens and three-way theme (UI1)

The first migration PR of `docs/52-console-ui-plan.md` (approved on PR #50; its section 14 lists
the decisions made from the code before this step). Presentation only: no layout, component,
behaviour, permission, route or API change.

## What changed

- **Tokens.** `frontend/src/styles/tokens.css` holds every colour of the page, direction A
  ("Quiet") of the mock-ups, under the plan's names, and the scales: type (system stack, 12 to
  22 px), spacing (4 px base), radius, elevation, motion, a scrim for dialogs. `styles.css` and
  `console.css` hold no colour of their own any more: the old names (`--page`, `--text-primary`,
  `--good`, `--warning`, `--critical`, `--banner`, ...) are gone. The charts' colours stay a data
  group (`--series-1`, `--series-2`, `--grid`, `--track`) that `TimeChart` reads as before.
- **Controls' borders** use `--border-strong` (3:1 against what is around them, WCAG 1.4.11); the
  old border was a 12% black.
- **Warning** is mock-up A's amber: the old yellow was 1.79:1 on the surface.
- **Theme: System, Light or Dark** (Q7), a native `<select>` labelled "Theme" in the header where
  the "Dark theme" button was. System is the default and follows the operating system live (a
  `matchMedia` listener). The hook stamps `data-theme="light"` or `"dark"` on `<html>`; before it
  runs, a `prefers-color-scheme` rule on `:root:not([data-theme])` gives a dark system the dark
  values, so the page never flashes light. `localStorage` keeps only the word, under the key it
  used before ("light" and "dark" saved before keep their meaning; a word it does not know is
  System).
- **Monospace figures** (Q1): a `figure` class (the monospace stack, tabular digits) on the cells
  holding counts, rates, latencies and ids: the Applications list, the keys table's ids, the
  charts' table view and the monitoring's Applications tab. Sizes and alignment unchanged.
- `prefers-reduced-motion` turns transitions and animations off everywhere.

## Checks added

- **Contrast** (`frontend/src/styles/contrast.test.ts`, with `contrast.ts`): every pair of colours
  the page draws one on the other, by role, in both themes: 25 text pairs at 4.5:1 or more, 27
  non-text pairs (control borders, the focus ring, status dots, chart lines, meter fills) at 3:1
  or more. The two dark blocks are pinned equal, and every colour of the light theme must be
  declared again for the dark one. A control checks the ratio itself (black on white is 21:1, the
  old yellow is under 3:1, #777 on white is 4.48:1 and is reported).
- **Bundle budget** (`frontend/scripts/bundle-budget.mjs`, `npm run budget`, CI's frontend job
  after the build): JS at most 135 kB and CSS at most 12 kB, each summed over its files and
  compressed with gzip at level 9 (1 kB = 1000 B).
- **Accessibility** (`frontend/e2e/a11y.ts`, `@axe-core/playwright` 4.14.0): axe's WCAG 2.0 and 2.1
  A and AA rules on every screen the e2e photographs (the specs' `picture` helpers) and on each
  monitoring tab, in the light and the dark system theme, which also checks that the page follows
  the system live. Each screen's findings go to the test output (`axe-<screen>.json`); a serious
  or critical finding fails the test; no rule is switched off. A control plants a nameless
  button, an image with no text and 4.48:1 text, and requires all three to be found in both
  themes and the check to fail on them.
- **Pictures in both themes**: the same helpers write `<name>-light.png` and `<name>-dark.png`.

## Measured

Bundle, by `npm run budget` after `npm run build`, on this machine (the sizes are byte counts, not
timings, and do not depend on the hardware):

| | JS raw | JS gzip | CSS raw | CSS gzip |
|---|---|---|---|---|
| Before (main at 7c5f805) | 391,300 B | 120,242 B | 12,204 B | 3,244 B |
| After (this PR) | see PR | see PR | see PR | see PR |

The axe results, the contrast table and the before and after bundle are in UI1's description,
with the commit they were measured at.

## Not built here

The frame, components, visual regression (moved to UI2: section 14, decision 8) and every later
step of the plan.
