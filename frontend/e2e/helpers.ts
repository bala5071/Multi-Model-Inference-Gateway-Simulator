import { expect, type Locator, type Page } from "@playwright/test";

export async function openTab(page: Page, name: string): Promise<void> {
  await page.getByRole("tab", { name }).click();
  await expect(page.getByRole("tab", { name })).toHaveAttribute("aria-selected", "true");
}

/** The Load section of the Playground tab, which is opened first. */
export async function loadSection(page: Page): Promise<Locator> {
  await openTab(page, "Playground");
  return page.getByRole("region", { name: "Load" });
}

export async function startPreset(page: Page, preset: string): Promise<void> {
  const load = await loadSection(page);
  await load.getByLabel("Preset").selectOption(preset);
  await load.getByRole("button", { name: /^(Start|Restart)$/ }).click();
  await expect(load.getByRole("status", { name: "Load status" })).toContainText(
    `Running “${preset}”`,
  );
}

export async function stopLoad(page: Page): Promise<void> {
  const load = await loadSection(page);
  const stop = load.getByRole("button", { name: "Stop" });
  if (await stop.isEnabled()) await stop.click();
  await expect(load.getByRole("status", { name: "Load status" })).not.toContainText("Running");
}

/** One chart panel of one model on the Capacity tab, with its table view switched on. */
export async function capacityPanel(page: Page, model: string, title: RegExp): Promise<Locator> {
  await openTab(page, "Capacity");
  const panel = page
    .getByRole("region", { name: `${model} capacity` })
    .getByRole("region", { name: title });
  await panel.getByRole("button", { name: "Table" }).click();
  // The chart's own legend is a table too; the table view is the one with a caption.
  await expect(panel.locator("table.data-table")).toBeVisible();
  return panel;
}

/** The values in a panel's table: its newest points, newest first. */
export async function tableValues(panel: Locator): Promise<number[]> {
  const cells = await panel.locator("table.data-table tbody td").allTextContents();
  return cells.map((text) => Number(text.replace(/,/g, "")));
}
