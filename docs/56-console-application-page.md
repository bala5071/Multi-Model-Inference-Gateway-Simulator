# 56. The Applications list, the application page, the monitoring on demand (UI4)

UI4 of `docs/52-console-ui-plan.md`, planned in its section 17 (the first commit of this PR) with
the answers in 17.7 and 17.8, following the approved flow of `docs/console-ia-v2-plan.md`
(sections 2.1 to 2.5). No backend change: no route, permission or answer of the gateway changed.

## The application page

- **Addresses** (2.2): `#/app/<id>/overview`, `/settings`, `/rules`, `/incidents`, `/keys`, and
  `#/app/<id>/incidents/<incident>`. `routes.ts` is still the one parser and printer, pure, with a
  round-trip test. **The old addresses keep working**: `#/application/<id>` is the Settings tab,
  `#/rules/<id>` the Alert rules tab, `#/incidents/<app>[/<incident>]` the Incidents tab,
  `#/app/<id>` the Overview, and `#/incidents` the organization's list. `#/application`, `#/rules`
  and `#/keys` without an application name none: the session's landing, or "not available" where
  no application allows that tab (as before).
- **Tabs**: links (each changes the address, so the browser's back works), drawn like tabs, the
  current one `aria-current="page"`. Each follows its own action of the capabilities answer
  (`APP_TAB_ACTIONS`: `applications.read`, `settings.read`, `alert_rules.read`, `incidents.read`,
  `application_keys.list`): on the application when the gateway named it, on any it named
  otherwise, and the tab's screen still asks the gateway, which refuses one the session cannot
  use. A tab the session cannot use is not drawn, and its address draws the landing with the
  one sentence and no request for its data.
- **The breadcrumb** says "Applications / name", then the tab; "Applications" links only for a
  session that may open the list.
- **The navigation**: the feature-first items (Application settings, Alert rules, API keys) and the
  application dropdown on their screens are gone. A session that may list its organization's
  applications opens one from the list; one that may not gets its applications by id in the
  sidebar's "Application" group. **The landing** (2.1): the list; the page of its one application
  for a session the gateway names exactly one; Home (which lists them) for several.
- The tabs are the existing screens without their dropdown and level-2 heading (the page's heading
  is the application). The organization's incident list stays its own screen; opening an incident
  goes to that application's Incidents tab.

## Overview

What the gateway returns for one application today, each figure with what it covers (17.7):

- its plan, mode in force, models and requests-a-minute limit (`GET .../applications/<id>`);
- its open incidents, counted per open state from its own incident list (every session that opens
  the page may read it);
- **its figures over the last 60 minutes** (requests, error rate, p50 to within about 19%) from
  the organization's metrics read, for a session the gateway lets read the organization's usage.
  They are single figures for the window, not a series, and the page says so; "too few to judge"
  is the dash with its tooltip, as on the list. An application's admin or viewer is told the
  figures are not available to this account yet, and nothing is asked for.
- **No chart and no sparkline**: the gateway returns no series for one application (the platform
  monitoring's series are the platform admin's, from loopback). The per-application read of
  `docs/console-ia-v2-plan.md` (PR 2, `usage.read_application`) is not built here.

## The Applications list

Search (name or id) and filters (mode, status) on **the page shown only**, said under them with
"Showing n of m"; status pills; sortable columns (the current page); the dash with its tooltip;
an empty state that says what an application is (Add application above it where the gateway allows
it); a skeleton while loading. `filterRows` is pure and tested.

## The monitoring

Its own item (`#/monitor/<tab>`, the platform admin, loopback, unchanged), redrawn with UI3's
components: its tabs are `ui/Tabs` (Home and End, ids from `useId`, a real tab panel), its stream's
state a status pill, the models' sections cards. The old `components/Tabs.tsx` is removed.

## Split by route, and the bundle gate

`React.lazy` and dynamic `import()`: the login, the frame, the list and Home load first; the
application page, Add application, Organization settings, Channels, the organization's incidents
and the monitoring (with uPlot) load when opened, from the same origin (the CSP's `script-src
'self'` holds; the e2e's CSP trial runs over the chunks).

| | Before (main 45091a9) | After |
|---|---|---|
| Initial download (gzip) | 132,803 B (the whole page) | 90,151 B |
| On demand (gzip) | none | 53,500 B in 12 chunks; the monitoring 34,168 B, the application page 5,759 B |
| Total (gzip, summed per file) | 132,922 B | 143,651 B |

**The gate (17.8)**: `npm run budget` reads the initial download from the built `index.html` (its
module script and the files Vite preloads for it) and holds it to **135,000 B** gzip; it fails too
if the monitoring or the application page is not a chunk of its own loaded later (pulled into the
initial download, or folded into the entry by a static import: mutation W12). Each on-demand chunk and the total are
printed, not gated. Why splitting could not keep the total under 135 kB: UI4's code adds about
5.2 kB (every chunk gzipped as one stream is 138,147 B, already over), and gzipping files apart
adds about 5.5 kB. `npm run budget:control` lowers each budget alone and requires the gate to fail
on that budget's own line (before, JS and CSS were lowered together, so a dead JS check passed:
mutation W10), and on a font file and an on-demand chunk loaded first. CSS (12 kB) and fonts (0 B) are unchanged.

## Found on the way

- Playwright's role lookup matches names by substring, so "Sections" also found the page's
  "Application sections": the e2e's lookups of the sidebar are exact.

## Checks

The `application-page` e2e project: the login fetches neither on-demand chunk and each comes when
opened, the CSP trial over them (an organization admin, the platform admin), the keyboard (search,
open an application, its tabs), a deep link to a tab from logged out (the old address too), and
each new state under axe in both themes. Per role (`shell.e2e.ts`): the sidebar, the landing, App
one's tabs each opened, a refused address; the application page at 390 px for every role.

## Not built here

The per-application metrics read and its charts (IA PR 2's backend), Clients (IA PR 3), costs.
