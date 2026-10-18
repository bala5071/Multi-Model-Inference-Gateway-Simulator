// Takes the frames of docs/img/demo.gif: a tour of every tab of the dashboard, from the
// gateway in simulated live mode (seed 7). A GIF has no sound.
//
// Everything in it is simulated: no model runs, and the times are simulated time. Every
// frame is the top of the page, so the simulated-mode banner is in all of them. The seed
// fixes what the gateway does; when each frame is taken is real time, so two runs give
// tours that differ in detail.

import { mkdirSync } from "node:fs";
import path from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { openTab, startPreset } from "./helpers";

const FRAMES = path.resolve(import.meta.dirname, "results/tour");
const FRAME_EVERY_MS = 400;

let frame = 0;

async function frames(page: Page, count: number): Promise<void> {
  // The pointer is parked off the charts, so no hover readout is in the frames.
  await page.mouse.move(0, 0);
  for (let i = 0; i < count; i++) {
    await page.screenshot({
      path: path.join(FRAMES, `${String(frame).padStart(3, "0")}.png`),
      clip: { x: 0, y: 0, width: 1280, height: 720 },
    });
    frame += 1;
    await page.waitForTimeout(FRAME_EVERY_MS);
  }
}

async function setSpeed(page: Page, speed: number): Promise<void> {
  const button = page
    .getByRole("status", { name: "Simulated live mode" })
    .getByRole("button", { name: `${speed}×` });
  await button.click();
  await expect(button).toHaveAttribute("aria-pressed", "true");
}

test("capture the dashboard tour", async ({ page }) => {
  mkdirSync(FRAMES, { recursive: true });
  await page.goto("/dashboard/");
  await expect(page.getByRole("article")).toHaveCount(3);
  // Let the steady load put some history on the charts first.
  await setSpeed(page, 20);
  await expect(page.getByRole("article", { name: "economy" })).not.toContainText(
    "no request finished",
  );
  await page.waitForTimeout(3000);
  await setSpeed(page, 5);

  // 1. The Overview under steady load, then its latency charts.
  await openTab(page, "Overview");
  await frames(page, 10);
  await openTab(page, "Latency");
  await expect(page.locator("canvas").first()).toBeVisible();
  await frames(page, 8);

  // 2. One streamed request from the Playground, at real time so the tokens can be seen
  //    arriving.
  await openTab(page, "Playground");
  await setSpeed(page, 1);
  const form = page.locator("form.request-form");
  // By the start of the label's text: "Model" is also one of the choices of "Choose by".
  await form
    .locator("label", { hasText: /^Tenant/ })
    .locator("select")
    .selectOption("acme");
  await form
    .locator("label", { hasText: /^Model/ })
    .locator("select")
    .selectOption("standard");
  await form
    .locator("label", { hasText: /^Max tokens/ })
    .locator("input")
    .fill("128");
  await frames(page, 2);
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByRole("status", { name: "Request status" })).toContainText("Streaming");
  await frames(page, 8);
  await expect(page.getByRole("status", { name: "Request status" })).toContainText("Finished", {
    timeout: 60_000,
  });
  await frames(page, 4);

  // 3. The overload preset, started from the Playground, and the Overview as it takes hold.
  await startPreset(page, "overload");
  await frames(page, 3);
  await setSpeed(page, 20);
  await openTab(page, "Overview");
  await frames(page, 26);

  // 4. Capacity and Events once premium has been marked degraded.
  await openTab(page, "Events");
  await page.getByLabel("Type").selectOption("state_change");
  await expect(
    page.getByRole("list", { name: "Events" }).getByRole("listitem").first(),
  ).toContainText("premium is degraded", { timeout: 60_000 });
  await page.getByLabel("Type").selectOption("");
  await openTab(page, "Capacity");
  await expect(page.locator("canvas").first()).toBeVisible();
  await frames(page, 8);
  await openTab(page, "Events");
  await frames(page, 8);

  // 5. What each tenant used, and the Overview again.
  await openTab(page, "Tenants");
  await frames(page, 6);
  await openTab(page, "Overview");
  await frames(page, 8);
});
