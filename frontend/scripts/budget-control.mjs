// The bundle gate's control (docs/53): run against the built files with a budget of 1000 B, the
// gate must fail. If it passes, it could not be trusted to stop a bundle over its real budget.

import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const gate = fileURLToPath(new URL("./bundle-budget.mjs", import.meta.url));
const run = spawnSync(process.execPath, [gate], {
  env: { ...process.env, BUNDLE_BUDGET_JS: "1000", BUNDLE_BUDGET_CSS: "1000" },
  encoding: "utf8",
});
if (run.status === 1 && run.stdout.includes("OVER")) {
  console.log("The gate fails over its budget, as it must.");
  process.exit(0);
}
console.error(`The gate did not fail over a 1000 B budget (exit ${String(run.status)}).`);
process.exit(1);
