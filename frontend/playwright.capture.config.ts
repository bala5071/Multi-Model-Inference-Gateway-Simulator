import { defineConfig } from "@playwright/test";
import base from "./playwright.config";

// Not a test run: takes the screenshots and the frames of the GIF in docs/img.
// Run it through `uv run python scripts/capture_demo.py`, which also assembles the GIF.

export default defineConfig({
  ...base,
  testMatch: "*.capture.ts",
  timeout: 180_000,
  use: {
    ...base.use,
    colorScheme: "light",
    viewport: { width: 1280, height: 800 },
    trace: "off",
  },
});
