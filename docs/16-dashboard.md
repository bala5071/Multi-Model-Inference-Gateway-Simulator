# 16 — Dashboard

This step adds a browser dashboard over the monitoring API of note 15. It reads; it does not
change anything in the gateway.

Code: `frontend/` (React, TypeScript, Vite, uPlot), the `/dashboard` mount and the
`last_event_id` stream parameter in `src/gateway/app.py`.
Tests: `frontend/src/**/*.test.ts(x)` (Vitest), `tests/test_dashboard.py`.

## Read this first

- **There is no authentication.** Anyone who can reach the gateway sees every tenant's spend,
  as with the API underneath.
- **The build is not tracked.** `/dashboard` answers 404 `dashboard_not_built` until
  `npm run build` has been run.
- **What it shows is whatever the gateway reports.** In simulator mode that is simulated
  time on illustrative profiles; with real models most of the Capacity tab is unavailable
  (note 15). The dashboard labels neither; note 12 still applies to how the numbers are read.
- **The Playground tab is described in note 17**, which added it.

## Running it

Node 20 is needed. All commands are run in `frontend/`.

```sh
npm ci
npm run build     # writes src/gateway/dashboard/
```

Then start the gateway (`make serve`) and open `http://127.0.0.1:8000/dashboard/`.

For development, with hot reload:

```sh
npm run dev       # http://localhost:5173/dashboard/
```

The dev server proxies `/v1`, `/metrics` and `/healthz` to `http://127.0.0.1:8000`;
set `GATEWAY_URL` for a gateway elsewhere.

`npm run check` runs ESLint, `prettier --check`, `tsc` and the tests. CI runs the same in
its `frontend` job, and then the build.

## The tabs

| Tab | Shows |
|---|---|
| Overview | One card per model: health, req/s, in flight, queue depth, batch occupancy, p99 total latency against the SLO, p99 TTFT |
| Latency | Per model, p50 and p99 lines for TTFT, inter-token latency and total latency |
| Capacity | Per model, batch size against its maximum, KV tokens against capacity, prefix-cache hit rate |
| Tenants | Plan, spend, token budget bar, requests, rejections by reason, timeouts, downgrades |
| Events | The live event feed, newest first, filtered by type, model and tenant |
| Playground | One streamed request, and the load controls (note 17) |

Things to know:

- **The SLO is drawn on total latency only.** The gateway's SLO is on p99 total latency
  (note 10); there is no TTFT SLO. The Overview card compares p99 total with the SLO and
  shows p99 TTFT beside it as a plain number.
- **Health has three states**: healthy, degraded, and "No SLO". A model without an SLO is
  not shown as healthy.
- **The card's req/s is the last second that has ended.** The second in progress would
  understate it.
- **The card's latencies cover the metrics window (five minutes by default); the Latency
  tab's lines are per second.** A per-second p99 over three requests is the slowest of the
  three, so the two can differ a lot.
- **An unavailable group is drawn as a hatched panel saying "Not available in this
  backend"**, in place of the number or chart. Nothing unavailable is shown as zero.
- **Times are relative.** The gateway's clock has no date, so charts run from "now"
  backwards and events say how long ago they happened, both on the gateway's clock.
- **While disconnected**, a banner says so and gives the time of the last update; what is
  on screen stays, dimmed.

## Charts

Every chart goes through one component, `ChartPanel`, which gives it:

- its **units**, in the title, on the y axis, in the legend and in the table;
- a **Pause** button, which holds the data as it was when pressed. The stream keeps
  running underneath and the chart jumps to the present when resumed;
- a **Table** button, which shows the newest 20 drawn points as a table;
- **downsampling** to at most 300 drawn points.

Details:

- **Downsampling combines consecutive seconds**, it does not skip them. 900 seconds become
  300 points of 3 s each, and the panel says "Each point covers 3 s". Latency, batch and KV
  lines keep the **largest** value of the seconds combined, so a one-second spike survives;
  the cache hit rate takes their mean.
- **A second with no data is a gap in the line**, not a zero and not a straight line across.
  A value with a gap on both sides is drawn as a dot.
- **The y axis starts at 0.** For batch and KV it runs to the capacity. For latency it fits
  the data, and stretches to include the SLO only if the SLO is within four times the
  largest value; otherwise the panel says "p99 SLO … is above the range drawn". A 30 s SLO
  over 40 ms latencies would otherwise flatten the lines into the axis.
- Two lines per chart at most, in fixed colours (p50 blue, p99 orange), checked for
  colour-blind separation and contrast in both themes.

uPlot was chosen over Recharts because it draws on a canvas: nine charts updating once a
second cost little. The price is that the chart itself cannot be tested in jsdom, so the
component tests replace it with a stand-in and check what it is given. The real charts were
looked at by hand, in both themes, against a running gateway under load.

## The stream

`useMonitorStream` holds one `EventSource` on `/v1/monitor/stream?history_s=900`.

- **It reconnects itself**, after 1 s, then 2 s, doubling up to 15 s, and back to 1 s once a
  connection has delivered a snapshot. `EventSource`'s own retry gives up on an HTTP error.
- **Five seconds without a snapshot counts as disconnected**, since the gateway pushes one
  every second. A connection that stays open but silent never raises an error otherwise.
- **Each connection asks for the whole history again**, which fills the gap left by the
  disconnection, and passes `last_event_id`, so events published meanwhile still arrive.
- **A restarted gateway is detected** (its clock, event numbering or a lifetime counter went
  backwards) and everything accumulated from the old process is dropped.

`last_event_id` is new in this step: the stream already resumed from the `Last-Event-ID`
header, but a browser only sends that header on its own automatic retry, not on a new
`EventSource`. The query parameter wins if both are given.

## Staying in step with the API

The API has no generated schema (its endpoints return plain JSON), so
`frontend/src/api/types.ts` mirrors note 15 by hand. Two checks keep it honest:

1. `tests/test_dashboard.py` builds two snapshots on `SimClock` (three simulated models with
   plans and a degraded premium; three models behind pass-through workers) and compares
   them with `frontend/src/test/fixtures/*.json`. If the API changes, it fails until the
   fixtures are regenerated:

   ```sh
   GATEWAY_UPDATE_FIXTURES=1 uv run pytest tests/test_dashboard.py
   ```

2. `frontend/src/test/fixtures.ts` assigns those files to the hand-written types, so
   `npm run typecheck` fails if the two disagree. The component tests render from the
   same files.

This catches a field that was renamed, removed or changed type. It does not catch a new
field the types do not mention.

## Design decisions

- **The data transforms are pure functions with their own tests**
  (`charts/transforms.ts`, `api/store.ts`, `api/derive.ts`); components only lay out what
  they return.
- **The tab is in the URL's hash** (`#/latency`), so the server needs no route but
  `/dashboard` and there is no router dependency.
- **Themes** follow the system setting until the viewer chooses, and the choice is kept in
  `localStorage`. Colours are CSS variables by role.
- **The build goes inside the Python package** (`src/gateway/dashboard/`), so a wheel
  contains it. It is git-ignored, and `pyproject.toml` lists it as a build artifact so
  that ignoring it does not also leave it out of the wheel.
- **No UI library and no state library.** One reducer holds the stream's state.

## Not in this step

The Playground tab (added in note 17), authentication, the Dockerfile (it does not build the dashboard, so the
image answers `dashboard_not_built`), per-tenant charts, choosing the time range, and tests
of the canvas drawing itself.
