// The bundle budget (docs/52, Q4): after `npm run build`, the built page's JavaScript and CSS,
// each summed over its files and compressed with gzip at level 9, must stay within 135 kB and
// 12 kB (1 kB = 1000 bytes). Prints the raw and gzip size of every file and the totals, then
// fails over the budget. Run by CI's frontend job after the build: `npm run budget`.
// Fonts have their own budget (docs/52, section 15): 0 B, raw. The page uses the system stack, so
// any font file in the build is over it; adding a self-hosted font is a decision with its number.

import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { gzipSync } from "node:zlib";

const ASSETS = new URL("../../src/gateway/dashboard/assets/", import.meta.url);
// The budget. The two variables exist for the gate's own control (budget-control.mjs) only.
const BUDGET = {
  js: Number(process.env.BUNDLE_BUDGET_JS ?? 135_000),
  css: Number(process.env.BUNDLE_BUDGET_CSS ?? 12_000),
  font: 0,
};
const FONT = /\.(woff2?|ttf|otf|eot)$/i;

const totals = { js: { raw: 0, gzip: 0 }, css: { raw: 0, gzip: 0 }, font: { raw: 0, gzip: 0 } };
const rows = [];
for (const name of readdirSync(ASSETS).sort()) {
  const kind = name.endsWith(".js")
    ? "js"
    : name.endsWith(".css")
      ? "css"
      : FONT.test(name)
        ? "font"
        : null;
  if (kind === null) continue;
  const bytes = readFileSync(join(ASSETS.pathname.replace(/^\/([A-Za-z]:)/, "$1"), name));
  const gzip = gzipSync(bytes, { level: 9 }).length;
  totals[kind].raw += bytes.length;
  totals[kind].gzip += gzip;
  rows.push(
    `${name.padEnd(48)} raw ${String(bytes.length).padStart(8)}  gzip ${String(gzip).padStart(7)}`,
  );
}
if (rows.length === 0) {
  console.error("No built files: run `npm run build` first.");
  process.exit(1);
}
console.log(rows.join("\n"));
let over = false;
for (const kind of ["js", "css"]) {
  const { raw, gzip } = totals[kind];
  const ok = gzip <= BUDGET[kind];
  over ||= !ok;
  console.log(
    `${kind.toUpperCase().padEnd(4)} raw ${raw} B, gzip ${gzip} B, budget ${BUDGET[kind]} B gzip: ${ok ? "ok" : "OVER"}`,
  );
}
{
  const { raw } = totals.font;
  const ok = raw <= BUDGET.font;
  over ||= !ok;
  console.log(`FONT raw ${raw} B, budget ${BUDGET.font} B: ${ok ? "ok" : "OVER"}`);
}
process.exit(over ? 1 : 0);
