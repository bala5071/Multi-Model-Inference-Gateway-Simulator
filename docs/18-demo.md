# 18 — Demo packaging

This step packages the dashboard with the gateway so that one command shows it working, adds
browser smoke tests, and puts pictures of it in the README.

Code: `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `frontend/playwright.config.ts`,
`frontend/e2e/`, `scripts/capture_demo.py`, the queue chart in
`frontend/src/tabs/Capacity.tsx`, the `e2e` and `docker` jobs in `.github/workflows/ci.yml`.
Pictures: `docs/img/`.

## Read this first

- **The Docker image and the compose file have not been built or run.** The development
  machine has no Docker. They were written from the commands that were run directly, and
  the `docker` CI job is the first place they will be exercised. CI itself has not run:
  nothing has been pushed.
- **The demo is simulated live mode** (note 17). No model runs; everything in it, and in the
  pictures, is simulated time on the calibrated profiles (note 01), with placeholder prices.
  Nothing in it is a measurement.
- **The smoke tests and the capture were run on one Windows laptop**, with the installed
  Chrome. They have not been run with the Chromium that Playwright downloads, which is what
  CI would use.

## One command

```sh
docker compose up --build
```

Then open <http://localhost:8000/dashboard/>. The compose file starts the gateway with
`GATEWAY_SIMULATED_LIVE=1`, speed 5, the `steady` preset and seed 7.

Without Docker, the same thing in three commands. This is how it was run here, at speeds 5
and 20:

```sh
uv sync
(cd frontend && npm ci && npm run build)
GATEWAY_SIMULATED_LIVE=1 GATEWAY_SIM_SPEED=5 GATEWAY_SEED=7 uv run uvicorn gateway.app:create_default_app --factory --port 8000
```

To see something happen: on the Playground tab choose the `overload` preset and press
Restart, then watch the premium card on the Overview. Its queue fills, it is marked
degraded, and requests that allow it are served by the standard model; the Events tab lists
each one.

## The image

Two stages. `node:20-slim` runs `npm ci` and `npm run build`; `python:3.12-slim` installs
the gateway with uv, as before, and copies the built dashboard to
`src/gateway/dashboard/`, where `create_default_app` looks for it. Node is not in the final
image.

- **`docker run` on the image alone is the plain simulator**, as it was. Simulated live mode
  is chosen by the compose file's environment, not baked into the image.
- **Updated by platform step 3 (`docs/21-persistence.md`):** the compose file also mounts
  the volume `gateway-data` at `/data` for the database and reads secrets from a git-ignored
  `gateway.env` (optional for the demo); the image carries `scripts/manage.py`.
- **The port is published on `127.0.0.1` only** (`-p 127.0.0.1:8000:8000`, and the same in
  the compose file). Inside the container the server must bind `0.0.0.0` to be reachable at
  all; a plain `-p 8000:8000` would then publish it on every interface of the host, and
  monitoring and load control are not authenticated. `tests/test_docker_publish.py` fails on
  the unsafe form anywhere in the docs.
- **`npm run build` type-checks first**, so a dashboard that does not compile fails the
  image build.
- `.dockerignore` keeps `frontend/node_modules` and any dashboard built on the host out of
  the build context: the image always builds its own.

## Smoke tests

`frontend/e2e/smoke.e2e.ts`, run with `npm run e2e` in `frontend/`. Playwright builds
nothing itself: the script builds the dashboard, and the config starts the gateway in
simulated live mode at speed 20 on port 8177 and stops it afterwards.

| Test | Checks |
|---|---|
| The page loads | The three model cards arrive over the stream, the page says "Live" and carries the simulated-mode banner with its seed and speed, every tab opens, a chart is drawn, and the page raised no error |
| A load preset changes the queue chart | With load stopped the premium queue chart's newest points are all 0; after starting `overload` from the Playground they exceed 10, and the Overview card shows the batch full (4 of 4 seats) |
| A downgrade event appears under overload | With `overload` running, the Events tab filtered to downgrades lists "A request for premium was served by …", a state change says premium is degraded, and the Overview card says Degraded |

Things to know:

- **The queue chart is read through its table view.** The chart is a canvas, which a test
  cannot read; the Table button shows the same points as text. The Capacity tab had no queue
  chart before this step, only the number on the Overview card; one was added.
- **Why these are not flaky by construction, and where they still could be.** What the
  gateway does is fixed by the seed, and at speed 20 a simulated minute is three seconds. The
  tests wait for a state (queue over 10, a downgrade listed), not for a time. They would
  fail on a machine so slow that simulated time crawls: each wait allows 60 real seconds
  for something that takes under 40 simulated ones.
- **They share one gateway and run in order**, because they change what it is doing.
- `PLAYWRIGHT_CHANNEL=chrome npm run e2e` uses an installed Chrome instead of
  `npx playwright install chromium`.
- Vitest does not pick these files up (`*.e2e.ts`), and `npm run check` does not run them.

On the development machine, on the tree this note was first committed with (parent commit
`cb42d66`), `PLAYWRIGHT_CHANNEL=chrome npx playwright test` passed all three in about seven
seconds.

## Pictures

```sh
uv run python scripts/capture_demo.py     # add PLAYWRIGHT_CHANNEL=chrome to use Chrome
```

It runs `frontend/e2e/demo.capture.ts` against the same simulated gateway (seed 7) and
writes `docs/img/`: `overview.png`, `latency.png`, `capacity.png`, `tenants.png`,
`events.png`, `playground.png`, and `overload.gif`, thirty frames of the Overview at speed 5
while the `overload` preset takes hold. The GIFs are assembled with Pillow, scaled to 960
pixels wide with 64 colours.

It then runs `frontend/e2e/demo.tour.ts` (`npm run tour`, `playwright.tour.config.ts`)
against a second gateway of its own, on port 8178 in simulated live mode with the `steady`
preset and seed 7, and writes `docs/img/demo.gif`: a silent tour of every tab, 91 frames
taken 400 ms apart (Pillow merges identical neighbours). Steady load on the Overview and
Latency tabs at 5x; one request of up to 128 tokens to the standard model, streamed at 1x
so the tokens can be seen arriving; then the `overload` preset at 20x until premium is
marked degraded, through the Overview, Capacity, Events and Tenants tabs. Every frame is
the top 1280 x 720 of the page, so the simulated-mode banner is in all of them. The tour
has a gateway of its own so that it always starts from the same calm state and does not
change what the stills see.

- **The pictures are of simulated live mode and say so in their own banner.**
- **A retake differs in detail.** The seed fixes what the gateway does; when each picture is
  taken is real time. Do not edit the pictures; retake them.
- The ones in the repository were taken on the development machine, with
  `PLAYWRIGHT_CHANNEL=chrome`, on the tree of the commit that added the tour (parent
  commit `879ee2f`). The first set showed invented profiles and
  was replaced (note 13).
- **Every picture shows the simulated-mode banner.** The Capacity picture is the whole page
  for that reason, rather than scrolled down to the premium model.

## Design decisions

- **The demo is the real server in a mode, not a separate demo app.** What the pictures show
  is the same code path a client reaches.
- **Smoke tests assert through accessible names and text**, the same roles the component
  tests use, so they do not depend on CSS classes (the one exception is telling the table
  view from the chart's own legend, which is also a table).
- **Pillow is now a declared development dependency.** It was already installed, through
  matplotlib; the capture script imports it directly.

## Not in this step

Running the image anywhere, publishing it, a compose service for real models, visual
regression tests, and smoke tests of real-model mode.
