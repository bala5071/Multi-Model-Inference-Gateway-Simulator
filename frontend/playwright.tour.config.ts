import { defineConfig } from "@playwright/test";
import base from "./playwright.config";

// Not a test run: takes the frames of the dashboard tour, docs/img/demo.gif, on a gateway
// of its own, so the tour starts from the same calm state every time and does not change
// what the stills in playwright.capture.config.ts see. Run it through
// `uv run python scripts/capture_demo.py`, which also assembles the GIF.

const PORT = 8178;

export default defineConfig({
  ...base,
  testMatch: "*.tour.ts",
  timeout: 180_000,
  use: {
    ...base.use,
    baseURL: `http://127.0.0.1:${PORT}`,
    colorScheme: "light",
    viewport: { width: 1280, height: 720 },
    trace: "off",
  },
  webServer: {
    command: `uv run uvicorn gateway.app:create_default_app --factory --port ${PORT} --log-level warning`,
    cwd: "..",
    env: {
      GATEWAY_SIMULATED_LIVE: "1",
      GATEWAY_SIM_SPEED: "5",
      GATEWAY_SIM_PRESET: "steady",
      GATEWAY_SEED: "7",
    },
    url: `http://127.0.0.1:${PORT}/healthz`,
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
