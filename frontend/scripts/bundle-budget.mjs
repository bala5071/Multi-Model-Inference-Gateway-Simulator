// The bundle budget (docs/52, Q4; docs/56): after `npm run build`, what the page downloads first
// must stay within 135 kB of JavaScript, compressed with gzip at level 9 (1 kB = 1000 bytes), and
// the page's CSS within 12 kB. Run by CI's frontend job after the build: `npm run budget`.
//
// Since UI4 the page is split by route: the login, the frame and the first screen load first; the
// application page, the organization's screens and the monitoring load on demand. So the JS
// budget is the **initial download**: the built index.html's module script and the files Vite
// preloads for it (its static imports), which is exactly what a browser fetches before the first
// screen. Each on-demand chunk and the total are printed, not gated: no page downloads the total
// at once (decided 2026-11-11, docs/52 section 17, with the measurements in docs/56). The gate
// also fails if a chunk that must load on demand (the monitoring, the application page) is ever
// pulled into the initial download.
//
// Fonts have their own budget (docs/52, section 15): 0 B, raw. The page uses the system stack, so
// any font file in the build is over it; adding a self-hosted font is a decision with its number.

import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";

const OUT = fileURLToPath(new URL("../../src/gateway/dashboard/", import.meta.url));
const ASSETS = join(OUT, "assets");
// The budget. The variables exist for the gate's own control (budget-control.mjs) only.
const BUDGET = {
  js: Number(process.env.BUNDLE_BUDGET_JS ?? 135_000),
  css: Number(process.env.BUNDLE_BUDGET_CSS ?? 12_000),
  font: 0,
};
const FONT = /\.(woff2?|ttf|otf|eot)$/i;
// Chunks that must never be in the initial download (docs/56): their names' prefixes.
const ON_DEMAND = (process.env.BUNDLE_ON_DEMAND ?? "App-,ApplicationPage-").split(",");

const names = readdirSync(ASSETS).sort();
if (!names.some((name) => name.endsWith(".js"))) {
  console.error("No built files: run `npm run build` first.");
  process.exit(1);
}

// What the page fetches first: its module script and the modules preloaded for it.
const html = readFileSync(join(OUT, "index.html"), "utf8");
const initial = new Set(
  [
    ...html.matchAll(
      /<(?:script[^>]*\ssrc|link[^>]*rel="modulepreload"[^>]*\shref)="[^"]*\/assets\/([^"/]+\.js)"/g,
    ),
  ].map((match) => match[1]),
);
if (initial.size === 0) {
  console.error("The built index.html names no script: the initial download cannot be read.");
  process.exit(1);
}

const size = (name) => {
  const bytes = readFileSync(join(ASSETS, name));
  return { raw: bytes.length, gzip: gzipSync(bytes, { level: 9 }).length };
};
const sum = (rows) =>
  rows.reduce((t, r) => ({ raw: t.raw + r.raw, gzip: t.gzip + r.gzip }), { raw: 0, gzip: 0 });
const line = (name, s, where) =>
  `${name.padEnd(48)} raw ${String(s.raw).padStart(8)}  gzip ${String(s.gzip).padStart(7)}  ${where}`;

const js = names.filter((n) => n.endsWith(".js")).map((n) => ({ name: n, ...size(n) }));
const css = names.filter((n) => n.endsWith(".css")).map((n) => ({ name: n, ...size(n) }));
const fonts = names.filter((n) => FONT.test(n)).map((n) => ({ name: n, ...size(n) }));

for (const row of js)
  console.log(line(row.name, row, initial.has(row.name) ? "initial" : "on demand"));
for (const row of css) console.log(line(row.name, row, "css"));

let over = false;
const first = sum(js.filter((row) => initial.has(row.name)));
const later = sum(js.filter((row) => !initial.has(row.name)));
const total = sum(js);
{
  const ok = first.gzip <= BUDGET.js;
  over ||= !ok;
  console.log(
    `JS   initial raw ${first.raw} B, gzip ${first.gzip} B, budget ${BUDGET.js} B gzip: ${ok ? "ok" : "OVER"}`,
  );
  console.log(
    `JS   on demand raw ${later.raw} B, gzip ${later.gzip} B (${js.length - initial.size} chunks, not gated)`,
  );
  console.log(`JS   total raw ${total.raw} B, gzip ${total.gzip} B (reported, not gated)`);
}
{
  const pulled = [...initial].filter((name) => ON_DEMAND.some((prefix) => name.startsWith(prefix)));
  const ok = pulled.length === 0;
  over ||= !ok;
  console.log(
    `JS   on-demand chunks kept out of the initial download: ${ok ? "ok" : `OVER (${pulled.join(", ")})`}`,
  );
}
{
  const { raw, gzip } = sum(css);
  const ok = gzip <= BUDGET.css;
  over ||= !ok;
  console.log(
    `CSS  raw ${raw} B, gzip ${gzip} B, budget ${BUDGET.css} B gzip: ${ok ? "ok" : "OVER"}`,
  );
}
{
  const { raw } = sum(fonts);
  const ok = raw <= BUDGET.font;
  over ||= !ok;
  console.log(`FONT raw ${raw} B, budget ${BUDGET.font} B: ${ok ? "ok" : "OVER"}`);
}
process.exit(over ? 1 : 0);
