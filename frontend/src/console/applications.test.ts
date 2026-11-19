import {
  applicationsAlice,
  capabilities,
  incidentCountsAlice,
  metricsAlice,
  metricsAliceRestart,
} from "../test/fixtures";
import {
  applicationRows,
  filterRows,
  applicationStatus,
  figureText,
  formatMs,
  modeText,
  openText,
  TOO_FEW,
  windowText,
  WINDOW_WORDS,
  type ApplicationRow,
  type StatusKind,
} from "./applications";
import type { Figure, OpenCounts } from "./types";

const ok: Figure = { value: 25, why_not: null };
const none: Figure = { value: 0, why_not: null };
const counts = (firing: number, pending: number, acknowledged: number): OpenCounts => ({
  application_id: "x",
  firing,
  pending,
  acknowledged,
});

describe("figureText", () => {
  it("writes each unit, and every reason for no value in words, never 0", () => {
    expect(figureText({ value: 25, why_not: null }, "count")).toBe("25");
    expect(figureText({ value: 20, why_not: null }, "percent")).toBe("20.0 %");
    expect(figureText({ value: 13.45, why_not: null }, "ms")).toBe("13 ms");
    // Too few: a dash, which the table explains (docs/54).
    expect(figureText({ value: null, why_not: "too_few" }, "percent")).toBe(TOO_FEW);
    expect(TOO_FEW).toBe(String.fromCodePoint(0x2014));
    expect(figureText({ value: null, why_not: "incomplete" }, "count")).toBe("incomplete");
    expect(figureText({ value: null, why_not: "unavailable" }, "ms")).toBe("unavailable");
    expect(figureText({ value: null, why_not: "above_range" }, "ms")).toBe(
      "above the measured range",
    );
    expect(figureText({ value: null, why_not: "something new" }, "ms")).toBe("not available");
    expect(figureText({ value: null, why_not: null }, "ms")).toBe("not available");
  });

  it("writes latencies in ms, then seconds", () => {
    expect(formatMs(1.19)).toBe("1.2 ms");
    expect(formatMs(180.4)).toBe("180 ms");
    expect(formatMs(2048)).toBe("2.0 s");
  });
});

describe("windowText", () => {
  it("always says the last 60 minutes, and since when after a restart", () => {
    expect(windowText(metricsAlice.window)).toBe(WINDOW_WORDS);
    expect(windowText(null)).toBe(WINDOW_WORDS);
    const since = metricsAliceRestart.window.since_ms;
    expect(since).not.toBeNull();
    expect(windowText(metricsAliceRestart.window)).toBe(
      `last 60 minutes (since ${new Date(since ?? 0).toISOString().slice(11, 16)} UTC, when the gateway restarted)`,
    );
  });
});

describe("applicationStatus", () => {
  it("is unknown when the counts or the evaluator cannot be read, or the evaluator is not ok", () => {
    expect(applicationStatus(null, "ok", ok).kind).toBe("unknown");
    expect(applicationStatus(undefined, null, ok).kind).toBe("unknown");
    expect(applicationStatus(undefined, "stale", ok).kind).toBe("unknown");
    expect(applicationStatus(counts(1, 0, 0), "disabled", ok).kind).toBe("unknown");
  });

  it("is firing, then attention, from the open incidents", () => {
    expect(applicationStatus(counts(1, 3, 0), "ok", ok)).toEqual({
      kind: "firing",
      words: "firing",
    });
    expect(applicationStatus(counts(0, 1, 0), "ok", ok).kind).toBe("attention");
    expect(applicationStatus(counts(0, 0, 1), "ok", ok).kind).toBe("attention");
  });

  it("is no requests, ok, or unknown from the request count", () => {
    expect(applicationStatus(undefined, "ok", none)).toEqual({
      kind: "no_requests",
      words: "no requests",
    });
    expect(applicationStatus(undefined, "ok", ok).kind).toBe("ok");
    expect(applicationStatus(undefined, "ok", { value: null, why_not: "incomplete" }).kind).toBe(
      "unknown",
    );
  });
});

describe("openText and modeText", () => {
  it("counts open incidents by state", () => {
    expect(openText(undefined)).toBe("0");
    expect(openText(null)).toBe("unknown");
    expect(openText(counts(2, 1, 0))).toBe("3 (2 firing, 1 pending)");
  });

  it("names each mode in words, and an unknown one as the gateway did", () => {
    expect(modeText("local_only")).toBe("local only");
    expect(modeText("local_with_hosted_fallback")).toBe("local, hosted fallback");
    expect(modeText("hosted_primary_with_local_fallback")).toBe("hosted first, local fallback");
    expect(modeText("future_mode")).toBe("future_mode");
  });
});

describe("applicationRows", () => {
  it("joins the list, the figures and the counts the gateway produced", () => {
    const rows = applicationRows(
      applicationsAlice,
      metricsAlice,
      incidentCountsAlice,
      "ok",
      capabilities.alice,
    );
    expect(rows.map((r) => r.id)).toEqual(
      applicationsAlice.applications.map((a) => a.application_id),
    );
    const shop = rows.find((r) => r.id === "north-shop");
    const blog = rows.find((r) => r.id === "north-blog");
    expect(shop).toMatchObject({
      mode: "local, hosted fallback",
      requests: "25",
      errorRate: "20.0 %",
      p50: "13 ms",
      open: "3 (2 firing, 1 pending)",
      status: { kind: "firing" },
      keys: true,
    });
    expect(blog).toMatchObject({
      mode: "local only",
      requests: "3",
      errorRate: TOO_FEW,
      p50: TOO_FEW,
      open: "1 (1 acknowledged)",
      status: { kind: "attention" },
    });
  });

  it("says unavailable, figure by figure, for what could not be read", () => {
    const rows = applicationRows(applicationsAlice, null, null, null, capabilities.vera);
    for (const row of rows) {
      expect(row).toMatchObject({
        requests: "unavailable",
        errorRate: "unavailable",
        p50: "unavailable",
        open: "unknown",
        status: { kind: "unknown" },
        // The organization's reader is not named with the keys' list.
        keys: false,
      });
    }
  });
});

describe("filterRows (docs/56)", () => {
  const row = (id: string, name: string, mode: string, kind: StatusKind): ApplicationRow => ({
    id,
    name,
    mode,
    requests: "0",
    errorRate: "0",
    p50: "0",
    open: "0",
    status: { kind, words: kind },
    keys: true,
  });
  const rows = [
    row("north-shop", "Shop", "local only", "ok"),
    row("north-blog", "Blog", "hosted first, local fallback", "firing"),
  ];

  it("keeps every row with no search or filter, in the page's order", () => {
    expect(filterRows(rows, { text: "", mode: "", status: "" })).toEqual(rows);
  });

  it("searches the name or the id, any case, and filters by mode and status", () => {
    expect(filterRows(rows, { text: "SHOP", mode: "", status: "" }).map((r) => r.id)).toEqual([
      "north-shop",
    ]);
    expect(filterRows(rows, { text: "north-b", mode: "", status: "" }).map((r) => r.id)).toEqual([
      "north-blog",
    ]);
    expect(filterRows(rows, { text: "", mode: "local only", status: "" }).map((r) => r.id)).toEqual(
      ["north-shop"],
    );
    expect(filterRows(rows, { text: "", mode: "", status: "firing" }).map((r) => r.id)).toEqual([
      "north-blog",
    ]);
    expect(filterRows(rows, { text: "shop", mode: "", status: "firing" })).toEqual([]);
  });
});
