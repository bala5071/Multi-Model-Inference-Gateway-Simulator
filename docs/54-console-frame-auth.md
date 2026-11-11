# 54. The console's frame and auth screens (UI2)

UI2 of `docs/52-console-ui-plan.md`, as reordered in its section 15 (the first commit of this PR).
Presentation, structure and accessibility only: no behaviour, route, permission or API change.

## The frame

- **Sidebar** (`src/shell/NavBar.tsx`): the items `navigation.ts` shows, drawn only from
  `GET /v1/auth/capabilities` as before, now grouped (Home; Organization; Application;
  Platform: each item's `group`) with Lucide icons. It narrows to icons; each link keeps its label
  as its name (visually hidden, with a tooltip), so the navigation reads the same either way. The
  choice is kept in this browser as one word (`gateway-sidebar`: `collapsed` or `expanded`).
  Its accessible name stays "Sections".
- **Top bar** (`src/shell/Header.tsx`): breadcrumbs, a pure function of the route
  (`crumbs` in `routes.ts`, tested for every route), and the **account menu**
  (`src/shell/UserMenu.tsx`): initials, then the name, the organization and the roles as words
  (they describe the menu), the theme (System, Light, Dark, as radio items) and Log out. Written
  to the WAI-ARIA menu button pattern: Enter, Space or ArrowDown open it on the first item,
  ArrowUp on the last; arrows, Home and End move; Escape closes it and gives the focus back; Tab
  closes it. The button's name is "Account: <user>", made of what it shows (axe's
  label-content-name-mismatch).
- **Skip to content**: the first control of the page, a button (a `#main` link would be read as a
  route), moving the focus to the content.
- **Phones** (under 768 px): the sidebar becomes a native `<dialog>` drawer opened from the top bar,
  drawn only while open, closed by Escape, its close button, or a followed link.
- **Without a session** (login, simulated live mode): the mark and the Theme select. **While a
  password must be changed**: the mark only; the screen has its own Log out.

## The auth screens

- A centred card: the mark, a heading, a helper line. Labelled fields with `autocomplete`
  `username`, `current-password` and `new-password`; the focus starts on the first field; Enter
  submits; the submit button shows its loading state (`aria-busy`, "Logging in...").
- **Show/hide** on every password field (`aria-pressed`); the value stays the caller's state and is
  emptied once sent, as before.
- **Caps Lock**: a warning tied to the field (`aria-describedby`) while it is on.
- **The password rules** are listed before anyone types and describe the new-password field. The
  gateway still decides; `src/console/passwordRules.ts` holds the numbers, and
  `tests/test_dashboard.py` pins each to the gateway's constant (`gateway.passwords`,
  `gateway.auth`).
- **Lockout**: the gateway answers every refused login alike (docs/27), so the page cannot and does
  not say an account is locked. After a refusal it shows that one answer and a fixed sentence of
  the rule: "After 5 failed attempts within 15 minutes, an account is locked for 15 minutes. Wait,
  or ask an administrator to unlock it." (pinned the same way).
- Nothing changed in what is sent or kept: the CSRF token in memory, no password, token or key in the
  address, storage or logs (the leak scans).

## The small things

- **"Too few to judge"** on the Applications list is a dash, with its meaning in a tooltip, in words
  for screen readers, and in a footnote under the table ("fewer than 20 requests for a rate, 10 for
  the p50", the alert signals' minimums, pinned).
- **"Add application"** is the primary button.
- **Toasts** (`src/console/Toasts.tsx`) replace the yellow notices for a change's result: a polite
  live region in the corner, dismissible, gone after 8 s. Errors stay beside their form. The notes
  about the form in front of you (the step-up warning, "shown once") stay where they are, as calm
  callouts.

## Libraries and the bundle

- **Lucide** (`lucide-react` 1.62.0, ISC, released 2026-10-27), imported icon by icon: the build
  holds only the icons used (checked: an imported icon's path is in the bundle, three others' are
  not). Recorded in `docs/licenses.md`.
- **No font file**: the system stack. The bundle gate's font budget is 0 B and fails on any font file;
  its control plants one and requires the gate to fail.
- No headless library: the account menu is written here.
- The bundle before and after is in UI2's description.

## Checks

- **CSP trial**: every role's session, through every item, the account menu and the sidebar, with a
  listener for content-security-policy violations and every request recorded: none blocked, nothing
  fetched from another origin.
- **Keyboard only** (e2e): log in, skip to the content, a sidebar link, the account menu and the theme,
  the collapse, log out, with no mouse.
- **axe** in both themes on every pictured screen, now including the login (empty, refused, password
  shown), the account menu open, the collapsed sidebar, a toast, and the phone's drawer.
- **Visual regression**: the `visual` project, compared with baselines in `frontend/e2e/visual/` by
  CI's `visual` job in `mcr.microsoft.com/playwright:v1.64.0-noble`; skipped elsewhere.
- The contrast test is unchanged: the frame draws only token pairs it already checks.

## Not built here

The components and the typed settings form (UI3), the Applications list and the application page,
then the monitoring (UI4).
