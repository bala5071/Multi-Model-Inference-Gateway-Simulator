// The bundle gate's control (docs/53): run against the built files with a budget of 1000 B, the
// gate must fail. If it passes, it could not be trusted to stop a bundle over its real budget.

import { spawnSync } from "node:child_process";
import { rmSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const gate = fileURLToPath(new URL("./bundle-budget.mjs", import.meta.url));
const run = spawnSync(process.execPath, [gate], {
  env: { ...process.env, BUNDLE_BUDGET_JS: "1000", BUNDLE_BUDGET_CSS: "1000" },
  encoding: "utf8",
});
if (run.status !== 1 || !run.stdout.includes("OVER")) {
  console.error(`The gate did not fail over a 1000 B budget (exit ${String(run.status)}).`);
  process.exit(1);
}

// And over the font budget: one planted font file, removed again whatever happens.
const planted = fileURLToPath(
  new URL("../../src/gateway/dashboard/assets/budget-control.woff2", import.meta.url),
);
writeFileSync(planted, "planted by budget-control.mjs");
let fonts;
try {
  fonts = spawnSync(process.execPath, [gate], { encoding: "utf8" });
} finally {
  rmSync(planted);
}
if (fonts.status !== 1 || !/FONT raw \d+ B, budget 0 B: OVER/.test(fonts.stdout)) {
  console.error(`The gate did not fail on a font file (exit ${String(fonts.status)}).`);
  process.exit(1);
}
// And when a chunk that must load on demand is in the initial download (docs/56): name the entry
// itself as one, and the gate must say it was pulled in.
const pulled = spawnSync(process.execPath, [gate], {
  env: { ...process.env, BUNDLE_ON_DEMAND: "dashboard-" },
  encoding: "utf8",
});
if (pulled.status !== 1 || !/kept out of the initial download: OVER/.test(pulled.stdout)) {
  console.error(
    `The gate did not fail on an on-demand chunk loaded first (exit ${String(pulled.status)}).`,
  );
  process.exit(1);
}
console.log(
  "The gate fails over its budgets (initial JS, CSS, a font file, an on-demand chunk loaded first), as it must.",
);
