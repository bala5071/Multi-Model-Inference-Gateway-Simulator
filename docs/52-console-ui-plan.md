# 52. Console UI pass: a design system before PR 2 (plan)

Status: **approved** (PR #50, 2026-11-11), with the answers in section 13, which override any
recommendation above that differs. Docs and three static mock-ups only in this PR. Presentation, structure and accessibility only: no behaviour, permission, route or API
change, no new feature. The design notes that ship with the code are numbered after this one.

Mock-ups to choose a direction from (open locally, no dependency; each has a System / Light /
Dark switch and works down to phone width; every value in them is invented for layout):

- [`console-ui-mockups/a-quiet.html`](console-ui-mockups/a-quiet.html): **A, Quiet.** A light
  canvas and a white sidebar, one blue accent, soft tinted status pills, 48 px rows.
- [`console-ui-mockups/b-dense.html`](console-ui-mockups/b-dense.html): **B, Dense.** A dark
  sidebar in both themes, compact 36 px rows, figures in monospace, status as a dot and a word,
  a summary strip over the table, a teal accent.
- [`console-ui-mockups/c-cards.html`](console-ui-mockups/c-cards.html): **C, Cards.** Stat
  tiles over the list, a tinted sidebar, larger radius and soft elevation, an indigo accent.

Rendered previews (Chrome, 1400 px light and dark, and 390 px phone) are in
[`console-ui-mockups/previews/`](console-ui-mockups/previews/), for reading on GitHub.

All three keep what the page already says: "last 60 minutes", "too few to judge" instead of 0,
the status word beside its colour, "sorting applies to this page".

## 1. Audit of the current screens

Read from `docs/pr-console-v2-screenshots/` (PR 1), `docs/pr-console-integration-screenshots/`
(P2), `docs/pr-c1-screenshots/`, `docs/pr-c2-screenshots/` and `docs/img/` (monitoring).

**Everywhere**

- **No frame.** The page is a header, a row of tab-like links and panels stacked down the page.
  There is no sidebar, no breadcrumb, no page header with its primary action, and the user, the
  organization and the role are one line of small text beside "Dark theme".
- **Navigation grows sideways.** The org_admin's bar holds eight items in one line (Home,
  Applications, Organization settings, Application settings, Alert rules, API keys, Channels,
  Incidents); organization pages and application pages are mixed with no grouping.
- **Controls look like browser defaults.** Buttons are small outlined boxes with no hierarchy:
  "Add application", "Revoke" and "Back to the list" look the same. Links are default blue and
  underlined. Selects and inputs are native, different heights from the buttons.
- **Type scale is flat.** h2 is large, everything else is 13 to 14 px with weight as the only
  difference; panel titles and table headers compete.
- **Panels everywhere.** Every section is a bordered card on a page that is itself a card,
  so nothing stands out; long screens (application settings) are one long column.
- **States are words, not components.** Loading is "Loading...", errors are red text, success
  is a yellow-bordered line that stays until the next change; nothing is announced or dismissed.
- **Theme toggle** stores the choice and then never follows the system again; there is no
  "system" option.
- **Dark theme** exists for the monitoring's tokens, but the console screens only partly use them.

**By role**

| Session | What it sees today | What looks unfinished |
|---|---|---|
| platform_admin | Home: a "Platform" heading, one "Monitoring" link, two sentences about `scripts/manage.py`; Monitoring | An almost empty page; the platform role's only real screen (monitoring) is a separate tab set with its own header and banner |
| org_admin | Applications (PR 1); settings, rules, channels, incidents | Applications is the best screen but its table header, status dot and buttons are unstyled; application settings is a 2,500 px column of raw JSON tables and a JSON editor; channels and incidents put lists and create forms in the same flat column |
| viewer (organization) | Applications without Add; the read screens | Read-only screens still show empty form chrome ("May change" lines, disabled controls); no visual cue that the page is read-only |
| app_admin | Home: "Your applications" with four links per application | A bulleted list of links as the main screen; the application's pages are scattered across four top-level items with a chooser on each |
| viewer (application) | Same as app_admin without keys | Same, read-only |
| no role | "No access yet" | Fine in content; it sits in an otherwise empty frame |
| logged out | Login card at the top left | Card pinned to the corner, no product framing, small "Log in" button |

**Monitoring:** model cards are the most finished part (status pills, stat values, bars), but
the simulated-live banner is a dense yellow paragraph, tabs differ from the console's, and the
cards do not share the console's components.

## 2. Layout

```
+--------------+---------------------------------------------------------------+
| [mark] Gateway| acme / Applications                  [org v]  [user  v]       |  top bar 56 px
|              +---------------------------------------------------------------+
| Home         | Applications                                  [+ Add application]|  page header
| ORGANIZATION | Requests, errors and latency over the last 60 minutes.        |
|  Applications|  +---------------------------------------------------------+  |
|  Channels    |  | table / cards / forms                                    |  |
|  Org settings|  +---------------------------------------------------------+  |
|  Incidents   |                                                               |
| APPLICATION  |                                                               |
|  (PR 2 tabs) |                                                               |
| PLATFORM     |                                                               |
|  Monitoring  |                                                               |
|  [< collapse]|                                                               |
+--------------+---------------------------------------------------------------+
```

- **Sidebar** (232 px, collapsible to a 64 px icon rail, the choice remembered per browser like
  the theme): the items `navigation.ts` already shows, grouped by level (Organization,
  Application, Platform). The groups come from each item's `Ask.level`, so the sidebar still
  draws only what `GET /v1/auth/capabilities` allows and holds no role name. Its accessible name
  stays "Sections" so the navigation tests keep their locator.
- **Top bar**: breadcrumbs (a pure function of the route, in `routes.ts`, tested), the
  organization as a label, and the user menu (a disclosure button: the user, the roles the
  gateway names, Theme: System / Light / Dark, Log out). For the platform owner, the space for
  the **client switcher** is reserved and empty: Clients and switching are PR 3 (no new route
  here).
- **Page header**: the title (the one `h2`, so the e2e headings hold, or `h1` with the tests
  moved in the same PR: Q10), a one-line description, and the screen's primary action on the
  right ("Add application"). Secondary actions sit in the content.
- **Responsive**: at 1200 px and up, the full sidebar; 768 to 1199 px, the icon rail with
  tooltips; under 768 px, the sidebar becomes a top bar with a menu button opening a drawer
  (the native `<dialog>`), tables scroll sideways with the first column sticky, page headers
  stack. On a phone every read view is usable; forms stay usable but are not tuned for it.

## 3. Design tokens

CSS custom properties on `:root`, redefined under `[data-theme="dark"]` and, when the
preference is System, under `@media (prefers-color-scheme: dark)`. One file,
`frontend/src/styles/tokens.css`. The scales below are common to the three directions; the
colour values are those of the chosen mock-up.

| Group | Tokens |
|---|---|
| Colour, surfaces | `--canvas`, `--surface`, `--surface-2`, `--border`, `--border-strong` |
| Colour, text | `--text`, `--text-2` (secondary), `--text-3` (tertiary, the lowest allowed) |
| Colour, accent | `--accent`, `--accent-text`, `--accent-soft` |
| Colour, status | `--ok`, `--warn`, `--bad`, `--idle`, each with `-soft` (pill background) |
| Spacing | 4 px base: `--s-1` 4, `--s-2` 8, `--s-3` 12, `--s-4` 16, `--s-5` 24, `--s-6` 32, `--s-7` 48 |
| Type | system stack (no font file); 12 / 13 / 14 (body) / 16 / 18 / 22 px; weights 400, 550, 650; line height 1.45; `font-variant-numeric: tabular-nums` for figures; monospace stack for ids |
| Radius | `--r-sm` 6, `--r-md` 8, `--r-lg` 12, `--r-pill` 999 |
| Elevation | `--e-0` none, `--e-1` hairline shadow (light only; dark uses borders), `--e-2` dialogs and menus |
| Focus | `--focus` ring: 2 px solid `--accent`, 2 px offset, on `:focus-visible` only, never removed |
| Motion | 120 ms for hover and toggles; none under `prefers-reduced-motion` |

**Contrast, checked:** the numbers here come from computing the WCAG 2 ratio of each pair the mock-ups use (text, secondary and tertiary text on surface and
canvas, accent on surface, the accent's text on the accent, each status colour on its pill and
on the surface), in both themes. The lowest used pair is 4.50:1 (C, tertiary text on its light
canvas); A's lowest is 4.75:1 (ok on its pill), B's is 4.80:1 (tertiary text on the header
band). The implementation makes this a **unit test**: it reads `tokens.css`, computes each
declared pair's ratio for both themes and fails under 4.5:1 for text and 3:1 for borders of
controls and the focus ring (WCAG 1.4.11).

**Theme:** the toggle becomes a three-way choice, System (the default, follows the OS live),
Light, Dark, in the user menu. `localStorage` keeps only that word, as it keeps "light" or
"dark" today.

## 4. Components

Written here, in `frontend/src/ui/`, each a small React component on native elements, styled by
the tokens through class names (no inline `style` attribute in markup, no CSS-in-JS: the CSP's
`style-src 'self'`), each with vitest tests for its roles, names and keyboard use.

| Component | Built on | Notes |
|---|---|---|
| Button | `<button>` | primary, secondary, quiet, danger; sizes sm and md; loading state keeps its width and says "Saving..." to screen readers |
| TextInput, Select, Textarea | native | label always visible; hint and error tied by `aria-describedby`; `aria-invalid` |
| Toggle | `<input type="checkbox" role="switch">` | for on/off settings only |
| Tabs | buttons with `role="tablist"` | arrow keys, Home/End; used by PR 2's application page |
| Table | `<table>` | sticky header; sortable columns as header buttons with `aria-sort`, sorting the **current page only**, said in the footer; empty, loading (skeleton rows) and error states; first column sticky on narrow screens |
| StatusPill | `<span>` | colour plus word, never colour alone |
| Card, StatTile | `<section>` | a tile is a label, a value, a note; "unavailable" and "too few to judge" are values too |
| Dialog, ConfirmDialog | native `<dialog>` with `showModal()` | the browser traps focus, makes the rest inert, handles Esc; destructive confirmations name the thing and the consequence, the destructive button is not the default |
| Toast | a region with `role="status"` (`role="alert"` for failures) | for the result of a change; dismissible; never holds a secret (a test), never the only place an error is shown |
| Skeleton | `<div aria-hidden>` plus a visually hidden "Loading..." | no shimmer under reduced motion |
| Inline validation | the field's error line | the gateway's message, shown after the gateway answers; the page adds no rule of its own |
| SecretReveal | read-only `<input>` in a panel | replaces `KeyShownOnce` and the channel's `SigningKey`: masked until "Show", a Copy button (the user's action only), "Shown once. It cannot be shown again.", cleared on "I have stored it" and on unmount; `autocomplete="off"`, `spellcheck="false"`; never in storage, the address or a log (the leak scans keep checking) |
| Tooltip | the `popover` attribute and `aria-describedby` | supplementary text only; opens on focus as well as hover |
| Pagination | buttons | "Previous / Next" and "1 to 50"; the gateway's cursors, unchanged |
| Menu (user menu) | a disclosure button and a list | Esc closes, focus returns to the button |
| Icon | inline SVG components written here | no icon set, nothing to license |

## 5. Charts for the Overview tab

**Recommendation: keep uPlot for time series, hand-written SVG for sparklines.**

- uPlot 1.6.32 (MIT) is already bundled and already wrapped by `ChartPanel` (units in the
  title, pause, table view, downsampling: the repo's rule that every chart goes through it).
  Using it for the application Overview costs **no new package and no new bytes**.
- uPlot draws on canvas. Accessibility comes from `ChartPanel`: the canvas is `aria-hidden`,
  each panel has a heading with its unit, a one-line text summary (latest value, window), and
  the **Table** button that shows the same points as a `<table>`; that button is reachable by
  keyboard. The plan adds the summary line, read by screen readers. The charts do not animate,
  so reduced motion needs nothing more.
- Sparklines in stat tiles, if PR 2 wants them, are a 40-line SVG `<polyline>` component with
  `role="img"` and an `aria-label` stating the latest value and the trend, no library.
- Considered: a React chart library (most are 40 kB gzip or more, and several inject style
  elements, which the CSP refuses); all-SVG charts (no zoom, no cursor, and a second chart code
  path beside uPlot).

## 6. Libraries

**Recommendation: write the components ourselves on native HTML (`<dialog>`, `popover`,
`<details>`, form controls). No headless component library, no new runtime package.**

| Option | For | Against |
|---|---|---|
| **Own components on native elements** (recommended) | No new package, nothing to license, smallest bundle; the browser does focus trapping (`showModal`), inertness and Esc; nothing injects `<style>` or sets inline style attributes in markup, so the strict CSP holds unchanged | We test keyboard and roles ourselves (vitest plus the axe check of section 8); fewer ready-made widgets (no combobox, no date picker: none is needed by the console) |
| Headless library A (React Aria, Apache-2.0) | Mature keyboard and screen-reader behaviour for every widget | A large dependency tree; its size is not measured here and would be measured before adoption |
| Headless library B (Radix Primitives, MIT) | Popular, small per component | Its dialog's scroll lock injects a `<style>` element at run time in the versions I know, which `style-src 'self'` would refuse: to be verified in a spike before it could be chosen |

Whatever is chosen, every package that ships in the bundle goes into `docs/licenses.md`'s "Built
into the dashboard and the console" table, which `tests/test_licenses.py` checks against
`frontend/package-lock.json`, with its size recorded in the PR. Development-only packages (the
accessibility checker of section 8) are recorded in a new "Development and test packages of the
frontend" table, also checked by that test (Q8).

## 7. Constraints that keep holding

- **CSP** unchanged: `default-src 'none'; script-src 'self'; style-src 'self'; ...`. No inline
  script or style, no external font, script or CDN; the system font stack, no font file. The
  e2e's CSP-violation listener stays and every new screen is run under it.
- **Bundle budget**, measured on `main` at 3fbbf46 with `npx vite build` (Vite's own gzip
  figures): JS 390.49 kB (121.08 kB gzip), CSS 12.20 kB (3.25 kB gzip). Proposed budget (Q4):
  **JS at most 135 kB gzip and CSS at most 12 kB gzip**, checked in CI by a small script that
  reads the built files after `npm run build` and fails over the budget; each PR states the
  before and after.
- **Leak scans**: the shell's, the alert screens' and the Applications spec's, with their planted
  controls, keep running on every converted screen; SecretReveal is added to them.
- **Per-role navigation tests** and the **capabilities-only rule**: the sidebar is drawn from the
  same `NAVIGATION` items and `allows`, the source lint against role names keeps running.
- **Every existing e2e** keeps passing; selectors by role and accessible name (not by class), so
  the conversion keeps names stable or changes a test in the same commit with the reason.
- **Simulated live mode**: the `SimulationBanner` stays on every monitoring tab, restyled, never
  removed.

## 8. Accessibility

- Everything works by keyboard: sidebar (Tab order, a skip link "Skip to content" first), menus
  (Esc, arrows), tabs (arrows, Home/End), tables (sort buttons in the header), dialogs (the
  browser's), toasts (focus is not moved; they are announced).
- A visible focus ring on every control, never removed (`:focus-visible`).
- Every control has a visible label; icon-only buttons (collapse, copy) have an accessible name.
- Toasts are announced (`role="status"`; failures `role="alert"`); inline errors are tied to their
  field; the page announces route changes by moving focus to the page header's title.
- `prefers-reduced-motion`: no transitions, no skeleton shimmer.
- **Automated check:** `@axe-core/playwright` (MPL-2.0, development only) run in the e2e on each
  role's landing and on every screen it opens, in both themes; the run fails on any "serious" or
  "critical" violation. Rules are not switched off without a line in the spec saying why.
  eslint-plugin-jsx-a11y (already used) keeps checking the source.

## 9. Visual regression

- Playwright's `toHaveScreenshot` on each role's landing and each screen a role opens (light
  and dark), from the e2e's own seeded database.
- **Against flakes:** baselines are produced and compared only in CI, in the pinned Playwright
  Linux image (the same fonts and rendering every time); local runs on other systems skip the
  comparison and say so. The clock in the page is fixed (`page.clock`), animations are
  disabled, the caret is hidden, values that change between runs (ids, times, figures, key
  ids) are masked; a small tolerance (`maxDiffPixelRatio` 0.002) absorbs anti-aliasing.
- Baselines are committed under `frontend/e2e/visual/`; refreshing them is a manual command
  whose diff is reviewed in the PR, never a way to make a failure pass (as with the perf gate).

## 10. Migration, as small stacked PRs

Each is a draft PR stacked on the previous once its CI is green, merged in order; base `main`
for the first.

| PR | What | Screens before -> after |
|---|---|---|
| **UI1. Tokens and theme** | `tokens.css`, base element styles, the contrast unit test, the three-way theme, the bundle budget script in CI, the axe check wired (on the screens UI4 to UI6 convert, the rest listed as pending), Playwright visual config | No layout change yet; colours and type move to the tokens; the theme toggle becomes System / Light / Dark |
| **UI2. The shell** | Sidebar (grouped, collapsible), top bar, breadcrumbs, user menu, page header, skip link, responsive drawer | Every screen gets the frame; the eight-item bar becomes the grouped sidebar; "Dark theme" and "Log out" move into the user menu |
| **UI3. Components** | Button, inputs, Toggle, Tabs, Table, StatusPill, Card, StatTile, Dialog, ConfirmDialog, Toast, Skeleton, SecretReveal, Tooltip, Pagination, Menu, Icon, each with tests; a dev-only gallery page served by `npm run dev` and not built | Nothing visible changes on its own; screens adopt them next |
| **UI4. PR 1's screens and the front door** | Login, password change, step-up dialog, Home, No access, Applications, Add application, API keys | Login centred with the product mark; Applications as the chosen mock-up; Add application as a two-step card; keys as a table with SecretReveal; revoke through ConfirmDialog; results as toasts |
| **UI5. The console's other screens** | Application and organization settings, alert rules, channels, incidents | Settings: the in-force table first, editing in a side panel instead of a second full table; presets as cards; channels and rules: list first, create in a dialog; incidents: list with pills, detail with a timeline |
| **UI6. Monitoring** | The monitoring tabs inside the shell, model cards on StatTile and StatusPill, `ChartPanel` restyled, the simulated banner as a compact notice | Same data and charts, the console's look; the banner keeps every word it must say |

Then PR 2 (the application page) is built on these.

## 11. Out of scope

New features, new routes (the client switcher's slot is empty until PR 3), permission changes,
vLLM, server-side sorting, a search box.

## 12. Questions

**Q1. Visual direction.** (A) Quiet; (B) Dense; (C) Cards; (D) a mix you name (for example A's
light frame with B's monospace figures).
**Recommendation: A**, with B's tabular monospace figures in tables: calm by default, dense
enough for many applications, and closest to the existing monitoring cards.

**Q2. Components.** (a) Our own on native HTML; (b) React Aria; (c) Radix Primitives (after a
CSP spike). **Recommendation: (a).**

**Q3. Charts.** (a) uPlot through `ChartPanel` plus SVG sparklines; (b) all hand-written SVG;
(c) a chart library. **Recommendation: (a)**: no new package, the accessible table view already
exists.

**Q4. Bundle budget.** (a) JS 135 kB gzip, CSS 12 kB gzip (about 14 kB of JS headroom over today);
(b) no growth at all over today's measurement plus 5%; (c) a budget per PR only, no CI gate.
**Recommendation: (a).**

**Q5. Visual regression.** (a) Pixel baselines in CI's pinned Linux image only, masked and with a
small tolerance; (b) baselines on every machine; (c) no pixel comparison, only the axe check and
the existing role tests. **Recommendation: (a).**

**Q6. Accessibility tool.** (a) `@axe-core/playwright` (MPL-2.0, already an allowed licence, dev
only); (b) eslint-plugin-jsx-a11y only (already in use). **Recommendation: (a).**

**Q7. Theme.** (a) System / Light / Dark, System by default; (b) keep today's two-way toggle.
**Recommendation: (a).**

**Q8. Development packages' licences.** (a) A new table in `docs/licenses.md` for frontend
development and test packages this work adds, checked by `tests/test_licenses.py`; (b) record
only what ships, as today. **Recommendation: (a)**, as you asked for every new package.

**Q9. Sorting.** (a) Sort the current page in the browser, said in the table's footer; (b) no
sorting until the gateway sorts. **Recommendation: (a)**: presentation only, no API change.

**Q10. Headings.** The e2e finds each screen by its level-2 heading. (a) Keep the page title an
`h2` under a visually hidden `h1` "Gateway"; (b) make the title `h1` and move every test in the
same PR. **Recommendation: (a)**: the tests and their meaning stay as they are.

## 13. Answers (approved 2026-11-11)

- **Q1: A (Quiet), with B's monospace figures in tables.** Mock-up A's colours are the tokens;
  table figures (counts, rates, latencies, ids) use the monospace stack with tabular numbers.
- **Q2: our own components on native elements.** Dialogs use the native `<dialog>` with
  `showModal()` and the platform's own focus handling (focus moved in, the rest inert, Esc,
  focus returned); menus follow the WAI-ARIA menu button pattern and tabs the tabs pattern
  (arrow keys, Home and End, roving tab index). **The axe checks cover every component**: each
  is rendered by the e2e (on a screen that uses it, or on the dev-only gallery page served to
  the e2e) and checked in both themes.
- **Q3: uPlot through `ChartPanel`, plus SVG sparklines.**
- **Q4: a CI gate at JS 135 kB gzip and CSS 12 kB gzip.**
- **Q5: pixel baselines in CI's pinned Linux image only.**
- **Q6: `@axe-core/playwright`.**
- **Q7: System / Light / Dark, System by default.**
- **Q8: a new table in `docs/licenses.md` for the frontend's development and test packages,
  checked by `tests/test_licenses.py`.**
- **Q9: sort the current page in the browser, labelled on the page.**
- **Q10: keep the page title at level 2**, under a visually hidden level-1 "Gateway".
- The decisions listed in #50's description are accepted.

## 14. UI1 (tokens and theme): decisions made from the code before it (2026-11-11)

Scope, as asked: the tokens, the three-way theme and the monospace figures, with the plan's UI1
checks (the contrast test, the bundle gate, the axe check). No layout or component change. Each
decision is listed in UI1's description so it can be objected to.

1. **One token file, the plan's names.** `frontend/src/styles/tokens.css` holds direction A's
   values under the plan's names (`--canvas`, `--text-2`, `--ok-soft`, ...); `styles.css` and
   `console.css` use only those. The charts' own colours stay as a data group (`--series-1`,
   `--series-2`, `--grid`, `--track`), read by `TimeChart` as today.
2. **The theme is resolved in script and stamped on `<html>`** (`data-theme="light"` or
   `"dark"`), as today; System follows the operating system live. Before the script runs, a
   `prefers-color-scheme` rule on `:root:not([data-theme])` gives the dark values, so a dark
   system never flashes light. The dark values are therefore written twice; a test pins the two
   blocks equal.
3. **The control is a native `<select>` labelled "Theme"** (System, Light, Dark) in the existing
   header, where the "Dark theme" button was. The user menu comes with UI2's frame.
   `localStorage` keeps only the word, under the same key; a saved "light" or "dark" from before
   keeps its meaning.
4. **Warning colours change hue.** The old bright yellow (`--warning`, #fab219) is 1.79:1 on the
   surface, under the 3:1 non-text minimum; mock-up A's amber replaces it on dots, borders and
   meters, as its words already sit beside it.
5. **Monospace figures** take a `figure` class on the cells that hold counts, rates, latencies
   and ids: the Applications list, the keys table's ids, the charts' table view and the
   monitoring's Applications tab. Alignment and sizes are unchanged (no layout change).
6. **The bundle gate** is `frontend/scripts/bundle-budget.mjs` (`npm run budget`), gzip level 9,
   1 kB = 1000 B, run by CI's frontend job after the build. Its own figures are the "before" and
   "after" of each PR (Vite's are printed beside them but not used).
7. **axe** (`@axe-core/playwright`, pinned) runs on every screen each role's e2e opens and on the
   simulated monitoring, in the light and dark system themes; serious or critical violations fail
   the run. A violation the tokens cannot fix stops this PR for a decision instead of being
   switched off.
8. **Visual regression moves to UI2.** UI1's pictures would be replaced by UI2's frame at once;
   the pinned-image job and its baselines start with the frame.
9. Design note: `docs/53-console-tokens-theme.md`.

## 15. The remaining UI work, reordered so the most visible comes first (2026-11-11)

Asked after UI1 (#51) merged. It replaces the order of section 10 from UI2 on; sections 1 to 9 and
13 still apply. Presentation, structure and accessibility only: no behaviour, route or permission
change in any of them.

**UI2. The frame and the auth screens.**

- A left sidebar for navigation: collapsible, icons with labels, grouped by level, drawn only from
  `GET /v1/auth/capabilities` as today. A top bar with breadcrumbs and a user menu (initials:
  the name, the organization, the roles as words, Theme, Log out). They replace the run-on header
  line and the flat row of links.
- Login and the forced password change redesigned: a centred card on a clean page, the product
  mark, a short heading and a helper line, labelled fields with the right `autocomplete`
  (`username`, `current-password`, `new-password`) so password managers work, a show/hide
  button, a caps-lock warning, the password rules shown before anyone types, inline errors in
  words, a loading state on submit, Enter to submit, focus on the first field, and a clear
  sentence about lockout. The forced change shows only its form and Log out, no navigation.
  Every security behaviour stays: CSRF, the one login refusal and its wording, nothing in the
  address, storage or logs.
- The small things from the screenshots: "too few to judge" shown as a dash with a tooltip,
  "Add application" as the primary button, the yellow notices replaced by toasts.

**UI3. Components and the typed settings form.** Buttons by role, tables (sticky header; empty,
loading and error states), status pills, cards, dialogs on the native `<dialog>`, toasts,
skeletons, menus and tabs; and the typed Application settings form: toggles for booleans,
number fields with units, chips for lists, a "changed" summary and a sticky Save bar, JSON
editing behind an "Advanced" disclosure. Every change is still checked by the gateway before it
is saved.

**UI4. The Applications list and the application page** (the approved flow: click an
application, then its tabs), **then the monitoring.**

**Libraries (decision).**

- No Bootstrap and no Tailwind.
- Icons: **Lucide** (`lucide-react`, ISC), imported icon by icon so only the icons used are
  bundled; pinned to a release at least two weeks old and recorded in the "Built into the
  dashboard and the console" licence table.
- Font: **the system stack**, no font file. Its size is 0 B; the bundle gate gains a separate
  font budget of 0 B (it fails on any font file in the build), so adding a self-hosted font later
  is a decision with its own number, not a side effect.
- A headless library only for a select, menu or combobox, and only if it passes a CSP trial (no
  style element, no inline style attribute, no request to another origin) with its licence in the
  table. UI2's user menu is written here on native elements, to the menu button pattern, so it
  needs none.
- Everything else stays our own, on native elements.

**What each PR carries:** axe in both themes on every screen and component state, keyboard-only
checks, the contrast test for every colour pair, the leak scans, the per-role e2e, mutation checks
restored by sha256, the visual-regression screenshots on CI's pinned Linux image, and screenshots
per role. The bundle is measured before and after against the 135 kB / 12 kB gate.

**Visual regression (from UI2, section 9):** a CI job in the Playwright image of the pinned
version (`mcr.microsoft.com/playwright:v<version>-noble`) runs a `visual` project with
`toHaveScreenshot`, values that change between runs masked, animations off; other machines skip
it. The first run writes the baselines and fails; they are taken from that run's artifact and
committed, and the next run compares against them. Refreshing them is a reviewed commit, never a
way to make a failure pass.

## 16. UI3: the components and the typed settings forms (plan, 2026-11-11)

Asked after UI2 (#52) merged. Sections 4, 7 to 9, 13 and 15 apply; where they meet the code, the
readings below. Presentation, structure and accessibility only: no behaviour, route, permission
or API change. **Two points are open (16.6) and are asked before any code.**

### 16.1 The components (`frontend/src/ui/`)

Section 4's table, with these readings:

- **Icons are Lucide** (section 15 replaced section 4's "Icon" row).
- **Buttons** by role: primary, secondary, danger, quiet; a loading state that keeps its width.
- **Fields** (text, number with its unit, select, textarea): a visible label, the hint and the
  error tied by `aria-describedby`, `aria-invalid`. The error is the gateway's answer, shown after
  it answers; a field adds no rule of its own.
- **Toggle**: `<input type="checkbox" role="switch">`. **Chips**: a list of removable values with
  an input to add one, each chip's remove button named after its value.
- **Tabs** (WAI-ARIA tabs: arrows, Home, End, roving tab index). First used by the Incidents
  screen's state filter, which today is a select: the same states and the same query.
- **Table**: sticky header; sortable columns as header buttons with `aria-sort`, sorting the
  current page only, said under the table (Q9); empty, loading (skeleton rows) and error states;
  always inside `TableScroll`.
- **StatusPill** (colour and word), **Card**, **StatTile**, **Skeleton** (no shimmer under reduced
  motion), **Tooltip** (`popover` and `aria-describedby`, opens on focus and hover), **Menu**
  (UI2's account menu moves here as the general menu button).
- **Dialog** and **ConfirmDialog** on the native `<dialog>` with `showModal()`. A destructive
  confirmation names the thing and the consequence, and its destructive button is not the default.
  Every destructive action that asks today (revoke a key, remove a channel, a recipient) goes
  through it. Asking where nothing asks today would change behaviour, so it is not added.
- **Toasts**: UI2's, moved here; `role="alert"` for a failure, which is also shown beside its
  form (never only in a toast).
- **Every component is rendered on a real screen** of this PR, so the e2e's axe runs (both themes)
  and the keyboard checks reach each one. No gallery page is added: it would be a new route.

### 16.2 The typed Application settings form

It replaces the JSON table of `EditForm.tsx`. **The gateway does not send a setting's kind.** The
form uses a copy of the name-to-kind map of `CATALOGUE` (`gateway/settings.py`), in
`src/console/settingKinds.ts`, pinned to the gateway's catalogue by `tests/test_dashboard.py` as
UI2 pinned the password rules. Only the kind is copied, never a bound: bounds and caps are the
gateway's answers. A setting missing from the copy falls back to a JSON field, so a new setting
in the gateway never breaks the page.

| Kind (gateway) | Control |
|---|---|
| `bool` | toggle |
| `int` | number field; the unit from the name's suffix (`_ms`: ms, `_per_minute`: per minute, `tokens`: tokens) |
| `tiers` | chips |
| `providers` | "All providers" or chips of provider ids, as the gateway's value is `"all"` or a list |
| `aliases` | rows of alias and tier, added and removed |
| `quality` | text field (the gateway's words, not an enum copied here) |

- Beside each setting: the value in force, its **source** and its **cap** (from the gateway's
  `effective` answer), and "Reset to the inherited value" (the gateway's `unset`).
- **"Changed" summary** before saving: each changed setting's old and new values, then the
  gateway's dry run (`validate`) as today: valid, refused with its reason, or "saving asks for
  your password". Save stays disabled until the dry run says valid.
- **A sticky Save bar** while anything is changed: the count, Discard, Save.
- **Presets as cards**: the values each sets, Apply (the same call and step-up as today).
- **History as a timeline**: version, fields changed, who, how, when; Revert to the previous
  version (the same call as today).
- **Advanced**: a `<details>` disclosure holding today's JSON editing, unchanged in what it sends.
- The draft, the change and the dry run stay in `derive.ts`'s pure functions (tested); the form
  only draws them.

### 16.3 The other screens

Alert rules, Channels, Incidents and API keys drawn with 16.1's components; the same calls,
the same probes (who may write is still asked of the gateway), the same accessible names where the
e2e reads them. Where a name changes, the test changes in the same commit, with the reason.

### 16.4 Checks this PR carries

Section 15's list. Screenshots per role at 1400 px light and dark **and at 390 px**. Visual
baselines from CI's pinned image, each looked at before it is committed. Bundle before and after.

### 16.5 Not in UI3

UI4 (the Applications list, the application page and its tabs, then the monitoring).

### 16.6 Open, asked before any code

1. **Organization settings.** The request says "the same for Organization settings", that is, the
   typed form there too. But that screen is read-only by decision: section 16 of
   `docs/settings-and-alerts-plan.md` gives it the caps, the applications each cap limits and the
   history, and note 47 says "It shows; it changes nothing." The gateway already allows the write
   (`org_settings.write` and `org_settings.loosen_egress` for an organization admin, with a dry run
   at `POST .../settings/validate` under the same permission), so an edit form changes no
   permission, but it gives the console a write it does not have today.
2. **The key shown once.** Section 4's SecretReveal masks a new API key or signing key until "Show".
   Today both are visible as soon as they are issued (with Copy and "I have stored it").
   Masking is a change on a secret's path.

### 16.7 Answers (2026-11-11)

1. **Organization settings: add the typed edit form.** The same form as the application's, on the
   organization's endpoints: the dry run (`POST .../settings/validate`) before Save, the step-up
   when it loosens egress (`org_settings.loosen_egress`), and the controls shown only when the
   gateway's dry run answers for this account (a refusal shows no Save). It is a new console write
   but no new permission. The caps, the applications each cap limits and the history stay on the
   screen. Note 47's "It shows; it changes nothing" is superseded by this answer (note 55 says so).
2. **The key shown once stays visible**, as today: restyled with the new components, with Copy
   and "I have stored it", cleared when it is stored or the screen is left. Section 4's masking is
   dropped.

## 17. UI4: the Applications list, the application page, the monitoring (plan, 2026-11-11)

Asked after UI3 (#53) merged, following the approved flow of `docs/console-ia-v2-plan.md`
(sections 2.1 to 2.5, 10 and 13). Sections 4, 7 to 9, 13, 15 and 16 of this plan still apply.
**Two points are open (17.6) and are asked before any code.**

### 17.1 The Applications list

The existing list (docs/51), drawn with UI3's components: a search box and filters (mode,
status) that work **on the current page only**, labelled so; status pills; the dash with its
tooltip for "too few to judge"; an empty state that says what an application is, with Add
application (shown only where the gateway allows it, as today); a skeleton while loading. The
figures, their window and their honesty rules (2.5) are unchanged.

### 17.2 The application page

`#/app/<id>/overview`, `/settings`, `/rules`, `/incidents`, `/keys` (2.2), with tabs and the
breadcrumb "Applications / name". Clicking a row of the list opens it. The tabs are the
existing screens under one header, so the application dropdown on Application settings, Alert
rules, Incidents and API keys goes. **The old hashes keep working**: `#/application/<id>` reads
as `#/app/<id>/settings`, `#/rules/<id>` as `/rules`, `#/incidents/<app>/<incident>` as that
application's incidents tab, `#/app/<id>/keys` is already the new form, and `#/incidents` stays
the organization's list. `routes.ts` stays the one parser and printer, with a round-trip test.
A tab the session cannot use is not drawn, and its address draws the landing with no request for
its data (10.1). The landing follows 2.1: one application for an application's admin or viewer
lands on that application's page.

### 17.3 Overview

Whatever the gateway returns for this application, said with what it covers, never more: see
17.6, question 1.

### 17.4 The bundle: split by route

JS is at 132.9 kB of 135 kB. The login, the frame and the first screen load first; the
application page, the Overview's charts (uPlot) and the monitoring load on demand
(`React.lazy` and dynamic `import()`, chunks from the same origin, so the CSP's
`script-src 'self'` holds). `npm run budget` gains an **initial-download** figure (the entry and
what it imports statically, gzip) with its own budget, checked in CI, beside the total; each
chunk is reported. The total budget is not raised.

### 17.5 What this PR carries

Section 15's list, plus deep links and direct URLs a role cannot use, per role, in the e2e.

### 17.6 Open, asked before any code

1. **Overview's data.** The plan's Overview (2.4) is per-application monitoring over time, from
   a read that does not exist yet: IA PR 2's per-application metrics route, with
   `usage.read_application` wired (marked LATER in `permissions.py` today). What the gateway
   returns now: the organization's metrics read (`usage.read_organization`, org admin and
   organization reader) gives each application's requests, error rate and p50 **over 60 minutes,
   as single figures, no series**; the incident counts by state. Time series exist only in the
   platform monitoring snapshot (15 minutes at 1 s, `monitoring.read`, platform admin, loopback),
   which the platform admin cannot use for an organization's application (IA Q1). An application's
   admin or viewer can read no figures at all today. So uPlot charts and sparklines on Overview
   cannot be drawn from what exists without inventing a series.
2. **"Fold the monitoring into the same page".** The monitoring is the platform's (models,
   scheduler, KV, events), for the platform admin from loopback. The reading proposed: it stays
   its own item (`#/monitor/<tab>`, unchanged rule), redrawn with UI3's components and loaded on
   demand, inside the same frame, not inside the application page.

### 17.7 Answers (2026-11-11)

1. **Overview uses what exists only.** Stat tiles of the 60-minute figures (requests, error rate,
   p50 to within about 19%) from the organization's metrics read, and the incident counts by
   state, each labelled with what it covers; for a session the gateway lets read them
   (`usage.read_organization`). No uPlot chart or sparkline on Overview: the gateway returns no
   per-application series. An application's admin or viewer is told the figures are not
   available to this account yet. The charts stay in the platform monitoring.
2. **The monitoring stays its own item** (`#/monitor/<tab>`, platform admin, loopback, unchanged),
   redrawn with UI3's components and loaded on demand, in the same frame.
