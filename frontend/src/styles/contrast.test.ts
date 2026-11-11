import { readFileSync } from "node:fs";
import { join } from "node:path";
import { block, contrast, themes } from "./contrast";

// Read from disk (the tests run in frontend/): the test runner hands CSS imports back empty.
const tokensCss = readFileSync(join(process.cwd(), "src", "styles", "tokens.css"), "utf8");

// Every pair of colours the page draws one on the other, by role. Text needs 4.5:1 (WCAG 1.4.3,
// normal-size text); the boundary of a control, the focus ring, a status dot and a chart line
// need 3:1 against what is around them (WCAG 1.4.11).
const TEXT: [string, string][] = [
  ...["canvas", "surface", "surface-2"].flatMap((bg): [string, string][] => [
    ["text", bg],
    ["text-2", bg],
    ["text-3", bg],
    ["accent", bg], // links
  ]),
  ["accent-text", "accent"], // a primary button's label
  ["accent", "accent-soft"], // the current navigation item
  ["text", "accent-soft"],
  ["ok", "ok-soft"],
  ["warn", "warn-soft"],
  ["bad", "bad-soft"],
  ["idle", "idle-soft"],
  ["text", "bad-soft"], // an error notice
  ["text", "warn-soft"],
  ["ok", "surface"],
  ["warn", "surface"],
  ["bad", "surface"],
  ["idle", "surface"],
];
const NON_TEXT: [string, string][] = [
  ...["canvas", "surface", "surface-2"].flatMap((bg): [string, string][] => [
    ["border-strong", bg], // inputs, selects, buttons
    ["focus", bg],
    ["ok", bg],
    ["warn", bg],
    ["bad", bg],
    ["idle", bg],
    ["series-1", bg],
    ["series-2", bg],
  ]),
  ["accent", "track"], // a meter's fill on its track
  ["warn", "track"],
  ["bad", "track"],
];

const { light, dark, darkBeforeScript } = themes(tokensCss);

function failures(theme: Record<string, string>, pairs: [string, string][], minimum: number) {
  return pairs
    .map(([fg, bg]) => {
      const a = theme[fg];
      const b = theme[bg];
      if (a === undefined || b === undefined) return `${fg} on ${bg}: not declared`;
      const ratio = contrast(a, b);
      return ratio < minimum ? `${fg} on ${bg}: ${ratio.toFixed(2)}` : null;
    })
    .filter((x) => x !== null);
}

describe("the tokens' contrast (WCAG 2 AA)", () => {
  it.each([
    ["light", light],
    ["dark", dark],
  ])("every text pair reaches 4.5:1 in the %s theme", (_name, theme) => {
    expect(failures(theme, TEXT, 4.5)).toEqual([]);
  });

  it.each([
    ["light", light],
    ["dark", dark],
  ])(
    "every control, focus, status and chart colour reaches 3:1 in the %s theme",
    (_name, theme) => {
      expect(failures(theme, NON_TEXT, 3)).toEqual([]);
    },
  );

  it("gives a dark system the same values before the theme hook runs", () => {
    expect(darkBeforeScript).toEqual(dark);
  });

  it("declares every colour of the light theme again for the dark one", () => {
    const colours = (t: Record<string, string>) =>
      Object.keys(t).filter((name) => /^#[0-9a-f]{6}$/i.test(t[name] ?? ""));
    expect(colours(block(tokensCss, ':root[data-theme="dark"]')).sort()).toEqual(
      colours(light).sort(),
    );
  });

  it("computes contrast as WCAG does (the control)", () => {
    expect(contrast("#000000", "#ffffff")).toBeCloseTo(21, 5);
    expect(contrast("#ffffff", "#ffffff")).toBeCloseTo(1, 5);
    // The old warning yellow on the old surface: under 3:1, so it was replaced (docs/53).
    expect(contrast("#fab219", "#fcfcfb")).toBeLessThan(3);
    expect(failures({ a: "#777777", b: "#ffffff" }, [["a", "b"]], 4.5)).toEqual(["a on b: 4.48"]);
  });
});
