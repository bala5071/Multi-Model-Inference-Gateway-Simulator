// Smoke tests: the built dashboard, served by the gateway, in simulated live mode.
//
// Everything the gateway does here is simulated time at 20 times real time, so "a minute
// of overload" is three real seconds. The waits below are for simulated things to happen;
// they are generous because a loaded CI machine may let simulated time through more slowly.

import { expect, test, type Page } from "@playwright/test";
import { capacityPanel, openTab, startPreset, stopLoad, tableValues } from "./helpers";

test.describe.configure({ mode: "serial" });

/** Every content-security-policy violation the page reports, gathered from its start. */
async function watchPolicy(page: Page): Promise<() => Promise<string[]>> {
  await page.addInitScript(() => {
    const seen: string[] = [];
    Object.assign(window, { policyViolations: seen });
    document.addEventListener("securitypolicyviolation", (event) => {
      seen.push(`${event.violatedDirective} ${event.blockedURI}`);
    });
  });
  return () =>
    page.evaluate(() => (window as unknown as { policyViolations: string[] }).policyViolations);
}

test("the page loads, is live, and says it is simulated", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    // The one expected error: /v1/auth/me answers 404 here, which is how the page knows this
    // mode has no accounts (docs/50). The browser reports every 404 it loads.
    const expected = message.location().url.endsWith("/v1/auth/me");
    if (message.type() === "error" && !expected) errors.push(message.text());
  });
  const violations = await watchPolicy(page);

  await page.goto("/dashboard/");

  // The one page, under the console's content security policy (docs/50).
  await expect(page.getByRole("heading", { name: "Gateway", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Monitoring" })).toBeVisible();
  const policy = await page
    .locator('head > meta[http-equiv="Content-Security-Policy"]:first-child')
    .getAttribute("content");
  expect(policy).toContain("style-src 'self'");
  expect(policy).not.toContain("unsafe-inline");
  await expect(page.getByText("Live", { exact: true })).toBeVisible();
  // One card per model, from the first snapshot of the stream.
  await expect(page.getByRole("article")).toHaveCount(3);
  await expect(page.getByRole("article", { name: "premium" })).toContainText("Batch occupancy");
  const banner = page.getByRole("status", { name: "Simulated live mode" });
  await expect(banner).toContainText("No model is running");
  await expect(banner).toContainText("Seed 7");
  await expect(banner.getByRole("button", { name: "20×" })).toHaveAttribute("aria-pressed", "true");

  // Every tab opens, keeps the label, and draws its charts.
  for (const tab of ["Latency", "Capacity", "Applications", "Events", "Playground"]) {
    await openTab(page, tab);
    await expect(banner).toBeVisible();
  }
  await openTab(page, "Latency");
  await expect(page.locator("canvas").first()).toBeVisible();
  expect(errors).toEqual([]);
  // Nothing the monitoring draws (uPlot's charts included) was blocked by the policy.
  expect(await violations()).toEqual([]);
});

test("simulated live mode has no accounts: the console's address says so, never a login", async ({
  page,
}, testInfo) => {
  // The console's old address keeps its place (the stub's script), then the page explains.
  await page.goto("/dashboard/console.html#/organization");
  await expect(page).toHaveURL(/\/dashboard\/#\/organization$/);
  const panel = page.getByRole("region", { name: "This mode has no accounts" });
  await expect(panel).toContainText("simulated live mode");
  await expect(page.getByRole("region", { name: "Log in" })).toHaveCount(0);
  await expect(page.getByRole("navigation", { name: "Sections" })).toHaveCount(0);
  // For the PR (git-ignored run output): this mode has no account, so no secret to show.
  await page.screenshot({ path: testInfo.outputPath("no-accounts.png"), fullPage: true });
  await panel.getByRole("link", { name: "Open the monitoring" }).click();
  await expect(page.getByRole("article")).toHaveCount(3);
  await expect(page.getByRole("status", { name: "Simulated live mode" })).toBeVisible();
});

test("a load preset changes the queue chart", async ({ page }) => {
  await page.goto("/dashboard/");

  // With no load the premium queue empties, and its chart's newest points are all zero.
  await stopLoad(page);
  let queue = await capacityPanel(page, "premium", /Queue depth/);
  await expect
    .poll(async () => Math.max(...(await tableValues(queue))), { timeout: 30_000 })
    .toBe(0);

  // The overload preset sends premium several times what it can serve.
  await startPreset(page, "overload");
  queue = await capacityPanel(page, "premium", /Queue depth/);
  await expect(queue.locator("canvas")).toBeVisible();
  await expect
    .poll(async () => Math.max(...(await tableValues(queue))), { timeout: 60_000 })
    .toBeGreaterThan(10);

  // The card on the Overview says the same thing.
  await openTab(page, "Overview");
  await expect(page.getByRole("article", { name: "premium" })).toContainText("4 of 4 seats");
});

test("a downgrade event appears under overload", async ({ page }) => {
  await page.goto("/dashboard/");
  await startPreset(page, "overload");

  await openTab(page, "Events");
  await page.getByLabel("Type").selectOption("downgrade");

  // Premium has to finish enough slow requests to be marked degraded first; after that,
  // requests that allow it are moved to a cheaper model.
  const feed = page.getByRole("list", { name: "Events" });
  await expect(feed.getByRole("listitem").first()).toContainText(
    /A request for premium was served by (standard|economy)\./,
    { timeout: 60_000 },
  );
  await page.getByLabel("Type").selectOption("state_change");
  await expect(feed.getByRole("listitem").last()).toContainText("premium is degraded");

  await openTab(page, "Overview");
  await expect(page.getByRole("article", { name: "premium" })).toContainText("Degraded");
});

test("the Playground sends a request as an application and shows the answer", async ({ page }) => {
  await page.goto("/dashboard/");
  await stopLoad(page);
  await openTab(page, "Playground");

  // In simulated live mode the gateway holds a throwaway key per application; the page
  // names the application and the key never reaches the browser.
  const form = page.locator("form.request-form");
  const application = form.locator("label", { hasText: /^Application/ }).locator("select");
  await expect(application).toBeVisible();
  await application.selectOption("acme");
  await form
    .locator("label", { hasText: /^Max tokens/ })
    .locator("input")
    .fill("4");
  const sent = page.waitForRequest((request) => request.url().endsWith("/v1/playground/generate"));
  await page.getByRole("button", { name: "Send" }).click();

  const request = await sent;
  expect(request.headers()["authorization"]).toBeUndefined();
  expect(request.postDataJSON()).toMatchObject({ application_id: "acme", max_tokens: 4 });
  await expect(page.getByText("Finished.")).toBeVisible({ timeout: 30_000 });
});
