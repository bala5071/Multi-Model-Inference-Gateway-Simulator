# 17 — Playground, load control and simulated live mode

This step makes the dashboard usable with nothing else running: a tab to send one request
and watch it stream, controls to put seeded load on the gateway, and a server mode that
replays a deterministic simulated workload at a chosen speed.

Code: `src/gateway/loadcontrol.py`, `PacedSimClock` in `src/gateway/clock.py`,
`src/gateway/workload.py` (moved, see below), the `/v1/control` endpoints and
`create_simulated_live_app` in `src/gateway/app.py`; in `frontend/`, the Playground tab,
`LoadControl` and `SimulationBanner`.
Tests: `tests/test_paced_clock.py`, `tests/test_loadcontrol.py`, `tests/test_live_sim.py`,
and in `frontend/` `api/playground.test.ts`, `tabs/Playground.test.tsx`, `App.test.tsx`.

## Read this first

- **Simulated live mode measures nothing.** No model runs. Every latency, rate and event in
  it is simulated time on the illustrative profiles of note 01. The dashboard says so in a
  banner on every tab.
- **Load control is off by default.** It lets anyone who can reach the gateway put load on
  it, and the gateway has no authentication.
- **Load started from the dashboard on an ordinary server runs on wall-clock time.** Its
  schedule is seeded and repeatable; the latencies it produces are not. Only simulated live
  mode is deterministic end to end.

## Running it

```sh
# Simulated live mode: models, load and time all simulated, at 20 times real time.
GATEWAY_SIMULATED_LIVE=1 GATEWAY_SIM_SPEED=20 uv run uvicorn gateway.app:create_default_app --factory

# An ordinary server whose dashboard may start and stop load.
GATEWAY_LOAD_CONTROL=1 uv run uvicorn gateway.app:create_default_app --factory
```

`make serve-sim` is the first of these at speed 5. Then open `/dashboard/` (note 16).

| Variable | Default | Meaning |
|---|---|---|
| `GATEWAY_SIMULATED_LIVE` | off | `1` selects simulated live mode. It cannot be combined with `GATEWAY_BACKEND=llamacpp`; the server refuses to start. |
| `GATEWAY_SIM_SPEED` | `1` | `1`, `5` or `20`: simulated seconds per real second. The dashboard can change it while running. |
| `GATEWAY_SIM_PRESET` | `steady` | The load preset started with the server. |
| `GATEWAY_SEED` | `0` | The seed of the simulated models and of the load. |
| `GATEWAY_LOAD_CONTROL` | off | `1` enables load control on an ordinary server. Simulated live mode always has it. |

## Playground

The tab sends one streaming request to `POST /v1/generate` and shows it arriving. It adds
nothing to the API; `EventSource` cannot POST, so it reads the stream with `fetch`.

- **Form**: tenant (from the plans; absent on a gateway without plans), model or quality,
  `allow_downgrade`, `max_tokens`, prompt.
- **Shown as it streams**: each token, time to first token, and one bar per token after the
  first whose height is the gap before it, in ms. Times are ms after the request arrived, on
  the gateway's clock (simulated ms in simulated live mode).
- **Headers**: `X-Model-Used`, `X-Downgraded-From` and `X-Cost-USD`, each with its value or
  "not sent".

**`X-Cost-USD` is never present on a streamed response.** The gateway sends headers before
the first token, and the cost is not known until the last (note 11). The playground shows
the header as not sent and gives the cost from the stream's final `done` event beside it.

A request refused before any token (403, 404, 429, 504) is shown with its status, code and
message. A failure after tokens have arrived is shown with the tokens kept.

## Load control

`LoadController` runs the load generator's workload inside the server. Each planned request
goes through the same `generate` handler an HTTP request reaches, so routing, budgets,
queue limits, downgrade, the ledger and monitoring all see it as they would any client.

### One generator

The planning half of `benchmarks/loadgen.py` (`LoadConfig`, `LengthBucket`,
`PlannedRequest`, `build_workload`) moved to `src/gateway/workload.py`, unchanged, so that
the server can import it; the package cannot import from `benchmarks/`. `loadgen.py`
re-exports the names. The benchmarks, the performance gate and their tests are unchanged
and pass.

### Presets

| Preset | Phases, repeated until stopped |
|---|---|
| `steady` | 240 requests at 8 req/s, the load generator's default model mix |
| `burst` | 40 requests at 2 req/s, then 300 at 30 req/s |
| `overload` | 180 requests at 6 req/s, 80% of them premium, with `allow_downgrade` |
| `long-prompts` | 120 requests at 4 req/s, 80% with prompts of 300 to 800 words, 80% sharing the system prompt |

They are illustrative, sized against the illustrative profiles.

- **A run is a function of its preset and seed.** Phase `n` of a run (counting from 0, across
  repeats) is `build_workload` with seed `seed * 1000 + n`. No two phases share a prompt
  except the common system prompt.
- **One run at a time.** Starting a run stops the current one and cancels its requests still
  in progress.
- **Tenants.** On a gateway with plans, each request goes to one of the tenants whose plan
  allows its model, in rotation by request index. With the packaged plans that includes
  `initech` on the free plan, whose 100,000-token budget a long run uses up; its requests
  are then refused with `budget_exhausted`. That is the budget working, and it shows on the
  Tenants and Events tabs.
- **A bound on requests in progress** (2,000). The default server's queue limits refuse
  long before it; without queue limits a request due while the bound is reached is skipped
  and counted.

### Endpoints

| Endpoint | Does |
|---|---|
| `GET /v1/control` | The state of load control and of simulation. Always present. |
| `POST /v1/control/load/start` | Body `{"preset": "...", "seed": n}`; `seed` defaults to the gateway's. 403 `load_control_disabled` if not enabled, 404 `unknown_preset`. |
| `POST /v1/control/load/stop` | Stops the run. 403 if not enabled. |
| `POST /v1/control/speed` | Body `{"speed": 1 | 5 | 20}`. 409 `not_simulated` outside simulated live mode, 422 `invalid_speed`. |

The three POSTs answer with the same body as `GET /v1/control`:

```json
{
  "seed": 7,
  "load": {
    "available": true,
    "presets": [{"name": "steady", "description": "..."}],
    "status": {"running": true, "preset": "steady", "seed": 7, "started_ms": 0.0,
               "phases_started": 1, "sent": 87, "served": 74, "refused": 0, "skipped": 0,
               "in_progress": 13}
  },
  "simulation": {"active": true, "speed": 5.0, "speeds": [1.0, 5.0, 20.0]}
}
```

(`frontend/src/test/fixtures/control-simulated.json`: simulated live mode, seed 7, ten
simulated seconds in. Produced and checked by `tests/test_dashboard.py`.)

With load control off, `load` is `{"available": false, "presets": null, "status": null}`;
outside simulated live mode, `simulation` is `{"active": false, "speed": null, "speeds":
null}`. `refused` counts every request not served: queue full, budget, timeout.

## Simulated live mode

`create_simulated_live_app` is the default simulated wiring (continuous batching, prefix
caches, chunked prefill, plans, queue limits, SLOs) on a `PacedSimClock`, with the load
preset started as the server starts.

### The clock

`PacedSimClock` is a `SimClock` that is held back to `speed` times real time.

- **Simulated time still jumps exactly to each wake-up**, as on `SimClock`. Real time decides
  only when a jump is allowed. So the simulated instant at which anything happens does not
  depend on the machine, its load, or the speed.
- **It does not sleep once per wake-up.** A simulated second has hundreds of wake-ups, and
  an operating system rounds a short sleep up, to about 15 ms on Windows. The first version
  slept per wake-up and ran at under 1x when asked for 20x. The clock now keeps the real time
  by which the current simulated time is due and sleeps only when it is ahead by a
  millisecond or more; a sleep that overran is made up by not sleeping for a while.
- **If the machine cannot keep up, it does not then rush.** All but the last 0.25 s of a
  delay is forgotten.
- **A long idle gap is taken in 20 ms slices**, with simulated time moving on at each. A
  request arriving from outside in the middle of a gap therefore sees a sensible time, and
  is served on time instead of after the rest of the gap.

Checked on the development machine, by reading `/healthz` twice about five seconds apart on
a server started with the first command above: simulated uptime advanced 101.9 s at speed
20 and 5.1 s at speed 1. That is one reading each, on one Windows laptop.

### What is deterministic

`test_the_same_seed_gives_the_same_event_sequence` runs the `overload` preset twice for 150
simulated seconds and compares every event (number, time, type, model, tenant, details) and
the whole monitoring snapshot (every series point, total and tenant). They are equal.
`test_a_different_seed_gives_a_different_sequence` and
`test_the_speed_changes_how_long_it_takes_and_nothing_else` cover the other two directions:
speed 1 and speed 20 give identical events and snapshots, and ask for 60 s and 3 s of real
waiting for 60 simulated seconds.

Those tests replace the real waiting with a function that returns at once. What they
establish is that nothing in the replay depends on real time.

**What is not deterministic**, and cannot be:

- **Anything a person does.** A Playground request, a preset change or a speed change takes
  effect at the simulated instant it happens to arrive at.
- **When a recovery is noticed.** The SLO monitor only re-evaluates when asked, and a
  snapshot asks (note 15). With a dashboard connected, a model's recovery can be stamped up
  to a second earlier than without one. Degradations are not affected: they are decided
  when a request finishes.

### In the dashboard

- A banner on every tab: "Simulated live mode. No model is running…", with the seed, the
  running preset, and the speed buttons. The header carries a "Simulated" badge.
- At 20x the stream pushes twenty snapshots a real second, since it pushes once a simulated
  second. Charts and the fifteen-minute window are in simulated time: at 20x the window
  passes in 45 real seconds.

## Design decisions

- **The whole gateway runs on the paced clock**, instead of a second, simulated gateway
  feeding the monitoring stream. There is one code path, and the Playground works against
  the simulated models too.
- **Arrivals are sent by one task, in order**, each at its planned time, not by one sleeping
  task per request as `loadgen` does for a finite run. A run here has no end.
- **Load is a server feature because the browser cannot be the generator**: its timers are
  throttled in a background tab, and its schedule would not be the load generator's.
- **Speeds are a fixed list** so that the dashboard and the server agree on them.

## Not in this step

Authentication for the control endpoints, saving or sharing a replay, stepping or pausing
simulated time, load presets from a file, per-preset tenants, and any real-model load test
(`benchmarks/loadgen.py --url` already does that from outside).
