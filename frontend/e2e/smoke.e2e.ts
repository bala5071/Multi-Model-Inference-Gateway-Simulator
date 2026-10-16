// Smoke tests: the built dashboard, served by the gateway, in simulated live mode.
//
// Everything the gateway does here is simulated time at 20 times real time, so "a minute
// of overload" is three real seconds. The waits below are for simulated things to happen;
// they are generous because a loaded CI machine may let simulated time through more slowly.

import { expect, test } from "@playwright/test";
import { capacityPanel, openTab, startPreset, stopLoad, tableValues } from "./helpers";

test.describe.configure({ mode: "serial" });

test("the page loads, is live, and says it is simulated", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));

  await page.goto("/dashboard/");

  await expect(page.getByRole("heading", { name: "Gateway monitor" })).toBeVisible();
  await expect(page.getByText("Live", { exact: true })).toBeVisible();
  // One card per model, from the first snapshot of the stream.
  await expect(page.getByRole("article")).toHaveCount(3);
  await expect(page.getByRole("article", { name: "premium" })).toContainText("Batch occupancy");
  const banner = page.getByRole("status", { name: "Simulated live mode" });
  await expect(banner).toContainText("No model is running");
  await expect(banner).toContainText("Seed 7");
  await expect(banner.getByRole("button", { name: "20×" })).toHaveAttribute("aria-pressed", "true");

  // Every tab opens, keeps the label, and draws its charts.
  for (const tab of ["Latency", "Capacity", "Tenants", "Events", "Playground"]) {
    await openTab(page, tab);
    await expect(banner).toBeVisible();
  }
  await openTab(page, "Latency");
  await expect(page.locator("canvas").first()).toBeVisible();
  expect(errors).toEqual([]);
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
