/// <reference types="vitest/config" />
import { resolve } from "node:path";
import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";

// Where the gateway listens while developing. `npm run dev` proxies the API to it.
const gateway = process.env.GATEWAY_URL ?? "http://127.0.0.1:8000";

// The admin console's content security policy (docs/47): its scripts and styles only from
// the gateway's own files, never inline; its requests only to the gateway. Added to the
// built console.html, not in development, where Vite injects its own inline styles.
export const CONSOLE_CSP = [
  "default-src 'none'",
  "script-src 'self'",
  "style-src 'self'",
  "img-src 'self' data:",
  "connect-src 'self'",
  "base-uri 'none'",
  "form-action 'none'",
].join("; ");

function consolePolicy(): Plugin {
  return {
    name: "console-content-security-policy",
    apply: "build",
    transformIndexHtml(html, context) {
      if (!context.filename.endsWith("console.html")) return html;
      return [
        {
          tag: "meta",
          attrs: { "http-equiv": "Content-Security-Policy", content: CONSOLE_CSP },
          injectTo: "head-prepend",
        },
      ];
    },
  };
}

export default defineConfig({
  plugins: [react(), consolePolicy()],
  // FastAPI serves the build at /dashboard (see docs/16-dashboard.md); the admin console is
  // its second page, /dashboard/console.html (docs/47).
  base: "/dashboard/",
  build: {
    outDir: "../src/gateway/dashboard",
    emptyOutDir: true,
    rollupOptions: {
      input: {
        dashboard: resolve(import.meta.dirname, "index.html"),
        console: resolve(import.meta.dirname, "console.html"),
      },
    },
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
