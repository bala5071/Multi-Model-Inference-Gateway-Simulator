import { randomBytes } from "node:crypto";
import { defineConfig } from "@playwright/test";

// Smoke tests of the built dashboard against the gateway in simulated live mode
// (docs/18-demo.md), and of the admin console against the plain server on a temporary
// database (docs/47). `npm run e2e` builds both pages first; the gateways are started here.
//
// PLAYWRIGHT_CHANNEL=chrome uses the Chrome already installed instead of the browser
// `npx playwright install chromium` downloads.

const PORT = 8177;
const CONSOLE_PORT = 8178;
// The console server's test mailbox (e2e/console_server.py): what its test SMTP server received.
const CONSOLE_MAIL_PORT = 8179;
process.env.CONSOLE_E2E_MAIL_PORT = String(CONSOLE_MAIL_PORT);

// The console's server needs a pepper, a vault key and accounts. Each is made here, at the
// start of the run, from the operating system's generator, and handed to the server and the
// tests through the environment only: never written to a file, never printed, never
// committed. The workers inherit them from this process, so every worker sees the same ones.
function generated(name: string, bytes: number): string {
  const value = process.env[name] ?? randomBytes(bytes).toString("base64url");
  process.env[name] = value;
  return value;
}

const consoleEnv = {
  GATEWAY_KEY_PEPPER: generated("CONSOLE_E2E_PEPPER", 32),
  GATEWAY_VAULT_KEY: generated("CONSOLE_E2E_VAULT_KEY", 32),
  GATEWAY_ADMIN_USERNAME: "operator-e2e",
  GATEWAY_ADMIN_PASSWORD: generated("CONSOLE_E2E_OPERATOR_PASSWORD", 18),
  // An account the operator creates must choose its own password at its first login: the
  // seed gives each its first password, and the tests change it to the second.
  CONSOLE_E2E_ADMIN_FIRST_PASSWORD: generated("CONSOLE_E2E_ADMIN_FIRST_PASSWORD", 18),
  CONSOLE_E2E_VIEWER_FIRST_PASSWORD: generated("CONSOLE_E2E_VIEWER_FIRST_PASSWORD", 18),
  CONSOLE_E2E_APPADMIN_FIRST_PASSWORD: generated("CONSOLE_E2E_APPADMIN_FIRST_PASSWORD", 18),
  CONSOLE_E2E_MAIL_PORT: String(CONSOLE_MAIL_PORT),
};
generated("CONSOLE_E2E_ADMIN_PASSWORD", 18);
// The alert screens' planted values (docs/49): a webhook URL's secret path and a recipient's
// address, each found by the leak scan where it must be and nowhere else.
generated("CONSOLE_E2E_HOOK_TOKEN", 18);
generated("CONSOLE_E2E_RECIPIENT_TAG", 6);
generated("CONSOLE_E2E_VIEWER_PASSWORD", 18);
generated("CONSOLE_E2E_APPADMIN_PASSWORD", 18);

export default defineConfig({
  testDir: "./e2e",
  // One gateway each, and the tests change what it is doing: they run one after another.
  workers: 1,
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  reporter: [["list"]],
  outputDir: "./e2e/results",
  use: {
    channel: process.env.PLAYWRIGHT_CHANNEL,
    viewport: { width: 1400, height: 1000 },
  },
  projects: [
    {
      name: "dashboard",
      testMatch: "smoke.e2e.ts",
      use: { baseURL: `http://127.0.0.1:${PORT}`, trace: "retain-on-failure" },
    },
    {
      name: "console",
      testMatch: "console.e2e.ts",
      // No trace: it would record the generated passwords, in the login and step-up bodies.
      use: { baseURL: `http://127.0.0.1:${CONSOLE_PORT}`, trace: "off", screenshot: "off" },
    },
    {
      // The alert screens (docs/49), on the same server, after the settings screens: those
      // test each account's first login.
      name: "console-alerts",
      testMatch: "console_alerts.e2e.ts",
      dependencies: ["console"],
      use: { baseURL: `http://127.0.0.1:${CONSOLE_PORT}`, trace: "off", screenshot: "off" },
    },
  ],
  webServer: [
    {
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
    {
      // The plain server on a database made for this run and seeded through the operator's
      // CLI, with the suite's test SMTP server beside it (e2e/console_server.py).
      command: `uv run python e2e/console_server.py ${CONSOLE_PORT}`,
      env: consoleEnv,
      url: `http://127.0.0.1:${CONSOLE_PORT}/healthz`,
      reuseExistingServer: false,
      timeout: 90_000,
    },
  ],
});
