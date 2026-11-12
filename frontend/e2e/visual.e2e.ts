// Visual regression (docs/52, sections 9 and 15; docs/54): the frame and the auth screens, per
// role, in both themes, compared with baselines taken on CI's pinned Playwright image. Fonts and
// rendering differ between machines, so it runs only where VISUAL=1 (the `visual` CI job, in
// mcr.microsoft.com/playwright at the version of package-lock.json); everywhere else it is
// skipped and says so. Against flakes: the run waits for a steady page (the evaluator ok, no toast,
// fonts loaded), animations are off, the caret is hidden, and what changes between runs (ids,
// times, figures, the evaluator's banner) is masked.

import { expect, test, type Locator, type Page } from "@playwright/test";
import {
  ADMIN,
  APP_ADMIN,
  APP_READER,
  NO_ROLE,
  OPERATOR,
  ORG_READER,
  logIn,
  logOut,
  type Account,
} from "./session";

// In order on the config's one worker, but not serial: a shot that differs does not skip the
// shots after it, so one run compares (and, for new ones, writes) every baseline (docs/56).
test.describe.configure({ mode: "default" });
test.skip(process.env.VISUAL !== "1", "visual baselines are compared on CI's pinned image only");

/** Waits until the alert evaluator says ok, so its banner is not drawn on one run and not another. */
async function evaluatorOk(page: Page): Promise<void> {
  await expect
    .poll(
      async () =>
        ((await (await page.request.get("/healthz")).json()) as { alert_evaluator?: string })
          .alert_evaluator,
      { timeout: 90_000, intervals: [1_000] },
    )
    .toBe("ok");
}

function masks(page: Page): Locator[] {
  return [
    page.locator(".figure"),
    page.locator(".evaluator-banner"),
    page.locator("time"),
    page.locator(".toasts"),
    // Only the boxes drawn: a closed <details> keeps a layout box, and its mask would cover
    // whatever follows it (docs/55).
    page.locator(".table-scroll:not(details:not([open]) *)"),
    page.locator(".muted").filter({ hasText: /UTC|restart|minutes/ }),
  ];
}

// Soft: a shot that differs is reported and the test goes on, so both themes and every later
// shot are taken and compared in the same run (docs/56).
/**
 * The whole page in both themes, masked; or, given `part` (a menu, a drawer: drawn above the
 * page, where a mask would cover it), that part alone, which holds nothing that changes.
 */
async function shot(
  page: Page,
  name: string,
  part?: Locator,
  partMasks: Locator[] = [],
): Promise<void> {
  await page.evaluate(() => document.fonts.ready);
  const steady = { animations: "disabled", caret: "hide", maxDiffPixelRatio: 0.002 } as const;
  for (const theme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
    await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
    if (part === undefined) {
      await expect.soft(page).toHaveScreenshot(`${name}-${theme}.png`, {
        ...steady,
        fullPage: true,
        mask: masks(page),
      });
    } else {
      await expect
        .soft(part)
        .toHaveScreenshot(`${name}-${theme}.png`, { ...steady, mask: partMasks });
    }
  }
  await page.emulateMedia({ colorScheme: "light" });
}

test("the login and the forced password change", async ({ page }) => {
  await evaluatorOk(page);
  await page.goto("/dashboard/");
  await expect(page.getByRole("region", { name: "Log in", exact: true })).toBeVisible();
  await shot(page, "login");
  // The bootstrap admin's first login asks for a new password: that screen, then on.
  const form = page.getByRole("region", { name: "Log in", exact: true });
  await form.getByLabel("Username").fill(OPERATOR.username);
  await form.getByLabel("Password", { exact: true }).fill(process.env[OPERATOR.first] ?? "");
  await form.getByRole("button", { name: "Log in" }).click();
  const change = page.getByRole("region", { name: "Change your password", exact: true });
  await expect(change).toBeVisible();
  await shot(page, "forced-password-change");
  // The bootstrap password works for one login only: finish the change for the tests after this.
  await change
    .getByLabel("Current password", { exact: true })
    .fill(process.env[OPERATOR.first] ?? "");
  await change.getByLabel("New password", { exact: true }).fill(process.env[OPERATOR.second] ?? "");
  await change.getByRole("button", { name: "Change password" }).click();
  await expect(page.getByRole("button", { name: /^Account:/ })).toBeVisible();
  await logOut(page);
});

const ROLES: [Account, string][] = [
  [OPERATOR, "platform-admin"],
  [ADMIN, "org-admin"],
  [ORG_READER, "organization-reader"],
  [APP_ADMIN, "app-admin"],
  [APP_READER, "application-reader"],
  [NO_ROLE, "no-role"],
];

for (const [who, name] of ROLES) {
  test(`${name}: the landing in the frame`, async ({ page }) => {
    await evaluatorOk(page);
    await page.goto("/dashboard/");
    await logIn(page, who);
    await expect(page.getByRole("button", { name: /^Account:/ })).toBeVisible();
    // The landing drawn, not its skeleton while its code or data arrive (docs/56).
    await expect(page.getByRole("heading", { level: 2 }).first()).toBeVisible();
    await expect(page.locator(".skeleton")).toHaveCount(0);
    await shot(page, `landing-${name}`);
    await logOut(page);
  });
}

test("the account menu, the collapsed sidebar, and the phone's drawer", async ({ page }) => {
  await evaluatorOk(page);
  await page.goto("/dashboard/");
  await logIn(page, ADMIN);
  await page.getByRole("button", { name: /^Account:/ }).click();
  await expect(page.getByRole("menu", { name: "Account" })).toBeVisible();
  await shot(page, "account-menu", page.locator(".menu-popup"));
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Collapse the sidebar" }).click();
  await shot(page, "sidebar-collapsed");
  await page.getByRole("button", { name: "Expand the sidebar" }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "Open the navigation" }).click();
  const drawer = page.getByRole("dialog", { name: "Navigation" });
  await expect(drawer).toBeVisible();
  await shot(page, "phone-drawer", drawer);
});

test("UI3: the typed settings forms, a confirmation, the incidents tabs (docs/55)", async ({
  page,
}) => {
  await evaluatorOk(page);
  await page.goto("/dashboard/");
  await logIn(page, ADMIN);
  const sectionsNav = page.getByRole("navigation", { name: "Sections", exact: true });
  // An application's tab (docs/56), by its address as a link does.
  const openTab = async (hash: string) => {
    await page.evaluate((h) => {
      window.location.hash = h;
    }, hash);
  };

  await openTab("#/app/app-two/settings");
  await expect(page.getByRole("region", { name: "Edit", exact: true })).toBeVisible();
  await shot(page, "settings-form");

  await sectionsNav.getByRole("link", { name: "Organization settings", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Organization settings", level: 2, exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("region", { name: "Edit", exact: true })).toBeVisible();
  await shot(page, "organization-form");

  await openTab("#/app/app-one/keys");
  await expect(page.getByRole("table", { name: "Keys" })).toBeVisible();
  await page
    .getByRole("button", { name: /^Revoke / })
    .first()
    .click();
  const dialog = page.getByRole("alertdialog", { name: "Revoke a key" });
  await expect(dialog).toBeVisible();
  // The whole dialog; its sentence names the run's key id, so that sentence is masked.
  await shot(page, "confirm-dialog", dialog, [dialog.locator(".dialog-body")]);
  await dialog.getByRole("button", { name: "Cancel" }).click();

  await sectionsNav.getByRole("link", { name: "Incidents", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Incidents", level: 2, exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("tab", { name: "Every state", exact: true })).toBeVisible();
  await shot(page, "incidents");
  await logOut(page);
});

test("UI4: the Applications list and an application's Overview (docs/56)", async ({ page }) => {
  await evaluatorOk(page);
  await page.goto("/dashboard/");
  await logIn(page, ADMIN);
  await expect(page.getByRole("table", { name: "Applications" })).toBeVisible();
  await shot(page, "applications-list");
  await page.evaluate(() => {
    window.location.hash = "#/app/app-one/overview";
  });
  await expect(page.getByRole("region", { name: "Figures", exact: true })).toBeVisible();
  await expect(page.locator(".skeleton")).toHaveCount(0);
  await shot(page, "application-overview");
  await logOut(page);
});
