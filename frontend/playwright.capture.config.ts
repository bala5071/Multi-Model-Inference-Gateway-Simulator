import { defineConfig } from "@playwright/test";
import base from "./playwright.config";

// Not a test run: takes the screenshots and the frames of the GIF in docs/img.
// Run it through `uv run python scripts/capture_demo.py`, which also assembles the GIF.

// The main config's simulated-live gateway, and none of its projects: each project names its own
// tests, which would replace this config's (the console's projects did, from C1 on).
const simulatedLive = Array.isArray(base.webServer) ? base.webServer[0] : base.webServer;

export default defineConfig({
  ...base,
  timeout: 180_000,
  projects: [
    {
      name: "capture",
      testMatch: "*.capture.ts",
      use: {
        ...base.use,
        baseURL: simulatedLive?.url?.replace(/\/healthz$/, ""),
        colorScheme: "light",
        viewport: { width: 1280, height: 800 },
        trace: "off",
      },
    },
  ],
  webServer: simulatedLive,
});
