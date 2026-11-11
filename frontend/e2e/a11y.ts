// The accessibility check of every screen the e2e opens (docs/52, Q6; docs/53): axe's WCAG 2.0
// and 2.1 A and AA rules, in the light and the dark system theme. The page's theme is System by
// default, so switching the emulated system setting also checks that the page follows it live.
// Every finding is written to the test's output (axe-<screen>.json) and printed as one line; a
// serious or critical one fails the test. No rule is switched off.

import { writeFileSync } from "node:fs";
import AxeBuilder from "@axe-core/playwright";
import { expect, type Locator, type Page, type TestInfo } from "@playwright/test";

const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];
const FAILING = new Set(["serious", "critical"]);

interface Finding {
  theme: string;
  rule: string;
  impact: string;
  nodes: number;
  help: string;
}

/** axe's findings on the page as it is, in the light and then the dark system theme. */
export async function findingsIn(page: Page, screen: string): Promise<Finding[]> {
  const findings: Finding[] = [];
  for (const theme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: theme });
    await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
    const result = await new AxeBuilder({ page }).withTags(TAGS).analyze();
    for (const violation of result.violations) {
      findings.push({
        theme,
        rule: violation.id,
        impact: violation.impact ?? "unknown",
        nodes: violation.nodes.length,
        help: violation.help,
      });
    }
    process.stdout.write(
      `axe ${screen} (${theme}): ${result.violations.length.toString()} violation(s), ` +
        `${result.passes.length.toString()} rule(s) passed
`,
    );
  }
  await page.emulateMedia({ colorScheme: "light" });
  return findings;
}

/** The check: every finding written to the test's output, none serious or critical. */
export async function checkAccessible(page: Page, testInfo: TestInfo, screen: string) {
  const findings = await findingsIn(page, screen);
  writeFileSync(testInfo.outputPath(`axe-${screen}.json`), JSON.stringify(findings, null, 2));
  expect(
    findings.filter((f) => FAILING.has(f.impact)),
    `serious or critical accessibility findings on ${screen}`,
  ).toEqual([]);
  return findings;
}

/**
 * A picture of the page in each theme (`<name>-light.png`, `<name>-dark.png`), then the axe check
 * in both. `masked` is covered in the pictures, never drawn (a key shown once).
 */
export async function picturedAndChecked(
  page: Page,
  testInfo: TestInfo,
  name: string,
  masked: Locator[] = [],
): Promise<void> {
  for (const theme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: theme });
    await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
    await page.screenshot({
      path: testInfo.outputPath(`${name}-${theme}.png`),
      fullPage: true,
      mask: masked,
      maskColor: "#555555",
    });
  }
  await checkAccessible(page, testInfo, name);
}
