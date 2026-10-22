// Takes the pictures in docs/img from the gateway in simulated live mode (seed 7).
//
// Everything in them is simulated: no model runs, and the times are simulated time. The
// seed fixes what the gateway does; when each picture is taken is real time, so two runs
// give pictures that differ in detail.

import { mkdirSync } from "node:fs";
import path from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { openTab, startPreset } from "./helpers";

const IMAGES = path.resolve(import.meta.dirname, "../../docs/img");
const FRAMES = path.resolve(import.meta.dirname, "results/frames");
const FRAME_COUNT = 30;
const FRAME_EVERY_MS = 400;

async function setSpeed(page: Page, speed: number): Promise<void> {
  const button = page
    .getByRole("status", { name: "Simulated live mode" })
    .getByRole("button", { name: `${speed}×` });
  await button.click();
  await expect(button).toHaveAttribute("aria-pressed", "true");
}

async function still(page: Page, name: string, fullPage = false): Promise<void> {
  // The pointer is parked off the charts, so no hover readout is in the picture.
  await page.mouse.move(0, 0);
  await page.screenshot({ path: path.join(IMAGES, `${name}.png`), fullPage });
}

test("capture the README pictures", async ({ page }) => {
  mkdirSync(IMAGES, { recursive: true });
  mkdirSync(FRAMES, { recursive: true });
  await page.goto("/dashboard/");
  await expect(page.getByRole("article")).toHaveCount(3);

  // A calm stretch first, so that the charts have a before.
  await page.waitForTimeout(4000);

  // The GIF: the Overview while the overload preset takes hold, at 5 times real time.
  await setSpeed(page, 5);
  await startPreset(page, "overload");
  await openTab(page, "Overview");
  for (let frame = 0; frame < FRAME_COUNT; frame++) {
    await page.screenshot({
      path: path.join(FRAMES, `${String(frame).padStart(3, "0")}.png`),
      clip: { x: 0, y: 0, width: 1280, height: 640 },
    });
    await page.waitForTimeout(FRAME_EVERY_MS);
  }

  // The stills, once a request has been downgraded.
  await setSpeed(page, 20);
  await openTab(page, "Events");
  await page.getByLabel("Type").selectOption("downgrade");
  await expect(
    page.getByRole("list", { name: "Events" }).getByRole("listitem").first(),
  ).toContainText("was served by", { timeout: 60_000 });
  await page.getByLabel("Type").selectOption("");
  await setSpeed(page, 1);
  await still(page, "events");

  await openTab(page, "Overview");
  await still(page, "overview");
  await openTab(page, "Latency");
  await expect(page.locator("canvas").first()).toBeVisible();
  await still(page, "latency");
  await openTab(page, "Capacity");
  // The premium model is the one under load, below the other two. The whole page is taken,
  // so that the simulated-mode banner at the top is in the picture with its charts.
  const premium = page.getByRole("region", { name: "premium capacity" });
  await expect(premium.locator("canvas").first()).toBeVisible();
  await still(page, "capacity", true);
  await openTab(page, "Applications");
  await still(page, "applications");

  // The Playground, after one streamed request to the economy model.
  await openTab(page, "Playground");
  // By the start of the label's text: "Model" is also one of the choices of "Choose by".
  const form = page.locator("form.request-form");
  await form
    .locator("label", { hasText: /^Application/ })
    .locator("select")
    .selectOption("acme");
  await form
    .locator("label", { hasText: /^Model/ })
    .locator("select")
    .selectOption("economy");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByRole("status", { name: "Request status" })).toContainText("Finished", {
    timeout: 60_000,
  });
  await still(page, "playground");
});
