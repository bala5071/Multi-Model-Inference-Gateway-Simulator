import { defineConfig } from "@playwright/test";

// Smoke tests of the built dashboard against the gateway in simulated live mode
// (docs/18-demo.md). `npm run e2e` builds the dashboard first; the gateway is started here.
//
// PLAYWRIGHT_CHANNEL=chrome uses the Chrome already installed instead of the browser
// `npx playwright install chromium` downloads.

const PORT = 8177;

export default defineConfig({
  testDir: "./e2e",
  testMatch: "*.e2e.ts",
  // One gateway, and the tests change what it is doing: they run one after another.
  workers: 1,
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  reporter: [["list"]],
  outputDir: "./e2e/results",
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    channel: process.env.PLAYWRIGHT_CHANNEL,
    viewport: { width: 1400, height: 1000 },
    trace: "retain-on-failure",
  },
  webServer: {
    // Simulated time at 20 times real time, so a minute of load takes three seconds.
    command: `uv run python -m gateway.serve --port ${PORT} --log-level warning`,
    cwd: "..",
    env: {
      GATEWAY_SIMULATED_LIVE: "1",
      GATEWAY_SIM_SPEED: "20",
      GATEWAY_SIM_PRESET: "steady",
      GATEWAY_SEED: "7",
    },
    url: `http://127.0.0.1:${PORT}/healthz`,
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
