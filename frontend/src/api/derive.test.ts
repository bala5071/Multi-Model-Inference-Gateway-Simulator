import { passthrough, simulated } from "../test/fixtures";
import {
  ago,
  ANY_EVENT,
  budgetUse,
  currentRate,
  describeEvent,
  distinct,
  eventTitle,
  filterEvents,
  health,
  occupancy,
} from "./derive";
import type { GatewayEvent, ModelSnapshot, TenantSnapshot } from "./types";

function model(snapshot: typeof simulated, name: string): ModelSnapshot {
  const found = snapshot.models[name];
  if (found === undefined) throw new Error(`fixture has no model ${name}`);
  return found;
}

const events = simulated.events.recent;

describe("health", () => {
  it("reads the SLO state", () => {
    expect(health(model(simulated, "premium").slo)).toBe("degraded");
    expect(health(model(simulated, "economy").slo)).toBe("healthy");
  });

  it("does not call a model without an SLO healthy", () => {
    expect(health(model(passthrough, "premium").slo)).toBe("no-slo");
  });
});

describe("currentRate", () => {
  it("is the rate of the last second that has ended", () => {
    // Premium's points end 5000 (final, 0 req/s) and 6000 (in progress).
    expect(currentRate(model(simulated, "premium").series)).toBe(0);
    // Economy's last final point is at 2000 ms, with 2 req/s.
    expect(currentRate(model(simulated, "economy").series)).toBe(2);
  });

  it("is null when no second has ended", () => {
    expect(currentRate(model(passthrough, "premium").series)).toBeNull();
    expect(currentRate([])).toBeNull();
  });
});

describe("occupancy", () => {
  it("is seated over seats", () => {
    expect(occupancy({ available: true, size: 6, max_size: 8 })).toBe(0.75);
    expect(occupancy(model(simulated, "premium").batch)).toBe(0);
  });

  it("is null, not zero, when the worker cannot say", () => {
    expect(occupancy(model(passthrough, "premium").batch)).toBeNull();
  });
});

describe("budgetUse", () => {
  const tenant = (used_tokens: number, budget_tokens: number): TenantSnapshot => {
    const acme = simulated.tenants.tenants?.acme;
    if (acme === undefined) throw new Error("fixture has no tenant acme");
    return { ...acme, used_tokens, budget_tokens };
  };

  it("grades the share of the budget used", () => {
    expect(budgetUse(tenant(10, 100))).toEqual({ ratio: 0.1, level: "ok" });
    expect(budgetUse(tenant(80, 100))).toEqual({ ratio: 0.8, level: "warning" });
    expect(budgetUse(tenant(100, 100))).toEqual({ ratio: 1, level: "exhausted" });
  });

  it("can pass the whole budget, since the last request admitted may overshoot", () => {
    expect(budgetUse(tenant(39, 30))).toEqual({ ratio: 1.3, level: "exhausted" });
  });

  it("treats a budget of nothing as used up", () => {
    expect(budgetUse(tenant(0, 0)).level).toBe("exhausted");
  });
});

describe("filterEvents", () => {
  it("returns everything, newest first, with no filter", () => {
    expect(filterEvents(events, ANY_EVENT).map((e) => e.seq)).toEqual([4, 3, 2, 1]);
  });

  it("filters by type, model and tenant together", () => {
    expect(filterEvents(events, { ...ANY_EVENT, type: "queue_full" }).map((e) => e.seq)).toEqual([
      2, 1,
    ]);
    expect(filterEvents(events, { ...ANY_EVENT, tenant: "acme" }).map((e) => e.seq)).toEqual([
      4, 2, 1,
    ]);
    expect(
      filterEvents(events, { type: "downgrade", model: "premium", tenant: "acme" }).map(
        (e) => e.seq,
      ),
    ).toEqual([4]);
    expect(filterEvents(events, { ...ANY_EVENT, model: "economy" })).toEqual([]);
  });

  it("does not match an event with no tenant when a tenant is asked for", () => {
    // The state change has tenant_id null.
    expect(filterEvents(events, { type: "state_change", model: "", tenant: "acme" })).toEqual([]);
  });

  it("lists the distinct values present, without nulls", () => {
    expect(distinct(events, "type")).toEqual(["downgrade", "queue_full", "state_change"]);
    expect(distinct(events, "model")).toEqual(["premium"]);
    expect(distinct(events, "tenant_id")).toEqual(["acme"]);
  });
});

describe("describeEvent", () => {
  const make = (type: string, data: Record<string, unknown>): GatewayEvent => ({
    seq: 1,
    at_ms: 0,
    type,
    model: "premium",
    tenant_id: null,
    data,
  });

  it("describes each of the gateway's own events", () => {
    expect(events.map(describeEvent)).toEqual([
      "premium refused a request: 2 waiting, limit 2.",
      "premium refused a request: 2 waiting, limit 2.",
      "premium is degraded: recent p99 2,910 ms is over its SLO.",
      "A request for premium was served by standard.",
    ]);
  });

  it("describes the other types", () => {
    expect(describeEvent(make("timeout", { timeout_ms: 120000 }))).toBe(
      "A request to premium was cancelled after 120,000 ms.",
    );
    expect(describeEvent(make("budget_exhausted", { used_tokens: 39, budget_tokens: 30 }))).toBe(
      "Refused: 39 of 30 budget tokens used.",
    );
    expect(describeEvent(make("state_change", { degraded: false, p99_ms: null }))).toBe(
      "premium recovered: nothing finished in its SLO window.",
    );
    expect(describeEvent(make("state_change", { degraded: false, p99_ms: 800 }))).toBe(
      "premium recovered: recent p99 800 ms.",
    );
  });

  it("shows the data of a type it does not know", () => {
    expect(describeEvent(make("something_new", { a: 1 }))).toBe('{"a":1}');
    expect(eventTitle("something_new")).toBe("something_new");
    expect(eventTitle("queue_full")).toBe("Queue full");
  });
});

describe("ago", () => {
  it("says how long before now, on the gateway's clock", () => {
    expect(ago(5910, 6782)).toBe("1 s ago");
    expect(ago(3000, 6782)).toBe("4 s ago");
    expect(ago(0, 125_000)).toBe("2 min 5 s ago");
    expect(ago(10, 5)).toBe("0 s ago");
  });
});
