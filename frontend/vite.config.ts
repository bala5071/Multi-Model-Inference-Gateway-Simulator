/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Where the gateway listens while developing. `npm run dev` proxies the API to it.
const gateway = process.env.GATEWAY_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  // FastAPI serves the build at /dashboard (see docs/16-dashboard.md).
  base: "/dashboard/",
  build: {
    outDir: "../src/gateway/dashboard",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/v1": gateway,
      "/metrics": gateway,
      "/healthz": gateway,
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    restoreMocks: true,
  },
});
