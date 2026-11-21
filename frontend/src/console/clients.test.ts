import {
  clientsNorth,
  clientsNorthShop,
  clientsNorthShop24h,
  clientsOrgs,
  clientsUsage,
  clientsUsage24h,
} from "../test/fixtures";
import { TOO_FEW } from "./applications";
import {
  bucketRows,
  clientApplicationRows,
  clientRows,
  healthItems,
  partial,
  spanText,
} from "./clients";
import type { ClientSpan } from "./types";

const WHOLE: ClientSpan = {
  name: "24h",
  bucket_ms: 3_600_000,
  first_ms: 0,
  end_ms: 86_400_000,
  since_ms: null,
  since_reason: null,
};

describe("the window in words (docs/57, 15.6 condition 3)", () => {
  it("says which window, and nothing more when it is whole", () => {
    expect(spanText(WHOLE, "24h")).toBe("last 24 hours");
    expect(spanText(null, "60m")).toBe("last 60 minutes");
    expect(partial(WHOLE)).toBe(false);
  });

  it("says since when, why, and that it is not whole, when the counts began inside it", () => {
    const since = clientsUsage24h.window.since_ms ?? 0;
    const iso = new Date(since).toISOString();
    expect(spanText(clientsUsage24h.window, "24h")).toBe(
      `last 24 hours, only since ${iso.slice(5, 10)} ${iso.slice(11, 16)} UTC, when the gateway restarted: not a whole 24 hours`,
    );
    expect(spanText(clientsUsage.window, "60m")).toBe(
      `last 60 minutes, only since ${iso.slice(11, 16)} UTC, when the gateway restarted: not a whole 60 minutes`,
    );
    expect(partial(clientsUsage24h.window)).toBe(true);
    const counted = { ...WHOLE, since_ms: 3_600_000, since_reason: "counting_began" };
    expect(spanText(counted, "24h")).toContain("when the gateway began counting them");
  });
});

describe("the organization rows", () => {
  it("joins the list and the usage by id, with each figure in words", () => {
    const rows = clientRows(clientsOrgs, clientsUsage, "ok");
    expect(rows.map((r) => r.id)).toEqual(["default", "north", "solo", "south"]);
    const north = rows[1];
    expect(north).toMatchObject({
      name: "North",
      kind: "business",
      enabled: true,
      applications: 2,
      requests: "29",
      errorRate: "10.3 %",
      fallbackRate: TOO_FEW,
      open: "1 (1 firing)",
    });
    expect(north?.status.kind).toBe("firing");
    expect(rows[3]?.status.kind).toBe("attention"); // south: one pending
    expect(rows[0]?.status.kind).toBe("no_requests");
  });

  it("says unavailable and unknown, never 0, when the usage could not be read", () => {
    const rows = clientRows(clientsOrgs, null, "ok");
    for (const row of rows) {
      expect(row.requests).toBe("unavailable");
      expect(row.open).toBe("unknown");
      expect(row.status.kind).toBe("unknown");
    }
  });

  it("is unknown whatever the incidents when the evaluator is not ok", () => {
    for (const evaluator of ["stale", "unknown", null]) {
      const rows = clientRows(clientsOrgs, clientsUsage, evaluator);
      expect(rows.every((row) => row.status.kind === "unknown")).toBe(true);
    }
  });
});

describe("a client's applications and one over time", () => {
  it("draws each application's figures and status", () => {
    const rows = clientApplicationRows(clientsNorth.applications, "ok");
    expect(rows.map((r) => [r.id, r.requests, r.status.kind])).toEqual([
      ["north-blog", "4", "ok"],
      ["north-shop", "25", "firing"],
    ]);
    expect(rows[0]?.errorRate).toBe(TOO_FEW);
  });

  it("lists the buckets newest first, minutes as HH:MM and hours with their day", () => {
    const minutes = bucketRows(clientsNorthShop);
    expect(minutes).toHaveLength(60);
    expect(minutes[0]?.when).toMatch(/^\d\d:\d\d$/);
    const starts = clientsNorthShop.buckets.map((b) => b.start_ms);
    expect(minutes[0]?.key).toBe(String(Math.max(...starts)));
    const hours = bucketRows(clientsNorthShop24h);
    expect(hours).toHaveLength(24);
    expect(hours[0]?.when).toMatch(/^\d\d-\d\d \d\d:00$/);
    // A bucket alone is judged alone: few requests is too few, never 0 %.
    expect(hours.some((row) => row.errorRate === TOO_FEW)).toBe(true);
  });
});

describe("the platform's health strip", () => {
  it("says each state and the totals in words", () => {
    const items = healthItems("ok", true, clientsUsage.totals);
    expect(items.map((i) => [i.label, i.value])).toEqual([
      ["Alert evaluator", "ok"],
      ["Stores", "ready"],
      ["Requests", "59"],
      ["Error rate", "5.1 %"],
      ["Fallback rate", "100.0 %"],
      ["Open incidents", "2 (1 firing, 1 pending)"],
    ]);
  });

  it("says unknown, never ok, for what could not be read, and not counted past the cap", () => {
    const items = healthItems(null, null, null);
    expect(items[0]).toMatchObject({ value: "unknown", tone: "warn" });
    expect(items[1]).toMatchObject({ value: "unknown", tone: "warn" });
    expect(items.slice(2).map((i) => i.value)).toEqual([
      "unavailable",
      "unavailable",
      "unavailable",
      "unknown",
    ]);
    expect(
      healthItems("stale", false, null)
        .slice(0, 2)
        .map((i) => i.tone),
    ).toEqual(["bad", "bad"]);
    const capped = healthItems("ok", true, {
      requests: { value: null, why_not: "above_cap" },
      error_rate: { value: null, why_not: "above_cap" },
      fallback_rate: { value: null, why_not: "above_cap" },
      open_incidents: null,
    });
    expect(capped[2]?.value).toBe("not counted: too many organizations");
    expect(capped[5]?.value).toBe("unknown");
  });
});
