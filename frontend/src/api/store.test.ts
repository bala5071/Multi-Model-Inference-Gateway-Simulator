import { passthrough, simulated } from "../test/fixtures";
import { addEvent, EMPTY_STATE, isRestart, MAX_EVENTS, mergePoints, reduce } from "./store";
import type { GatewayEvent, ModelPoint, Snapshot } from "./types";

function event(seq: number): GatewayEvent {
  return { seq, at_ms: seq * 10, type: "timeout", model: "m", application_id: null, data: {} };
}

describe("mergePoints", () => {
  const at = (t_ms: number, v: number) => ({ t_ms, v });

  it("appends new seconds and replaces ones already held", () => {
    const held = [at(0, 1), at(1000, 2)];
    // The stream sends the second that just ended again, now final, and the new one.
    expect(mergePoints(held, [at(1000, 20), at(2000, 3)], -1)).toEqual([
      at(0, 1),
      at(1000, 20),
      at(2000, 3),
    ]);
  });

  it("drops what is at or before the oldest time kept", () => {
    const held = [at(0, 1), at(1000, 2), at(2000, 3)];
    expect(mergePoints(held, [at(3000, 4)], 1000)).toEqual([at(2000, 3), at(3000, 4)]);
  });

  it("sorts, whatever order the points arrive in", () => {
    expect(mergePoints([at(2000, 3)], [at(0, 1), at(1000, 2)], -1).map((p) => p.t_ms)).toEqual([
      0, 1000, 2000,
    ]);
  });

  it("does not change its arguments", () => {
    const held = [at(0, 1)];
    mergePoints(held, [at(0, 9)], -1);
    expect(held).toEqual([at(0, 1)]);
  });
});

describe("snapshots", () => {
  /** The fixture as it would be `seconds` later, with one new point per model. */
  function later(seconds: number): Snapshot {
    const at_ms = simulated.at_ms + seconds * 1000;
    const models = Object.fromEntries(
      Object.entries(simulated.models).map(([name, model]) => {
        const last = model.series.at(-1) as ModelPoint;
        const added = { ...last, t_ms: last.t_ms + seconds * 1000, arrived: 7 };
        return [name, { ...model, series: [{ ...last, final: true }, added] }];
      }),
    );
    return { ...simulated, at_ms, models };
  }

  it("keeps the first snapshot's series and events", () => {
    const state = reduce(EMPTY_STATE, { kind: "snapshot", snapshot: simulated });
    expect(state.snapshot).toBe(simulated);
    expect(state.modelSeries.premium?.map((p) => p.t_ms)).toEqual([0, 3000, 4000, 5000, 6000]);
    expect(state.applicationSeries.acme?.length).toBe(
      simulated.applications.applications?.acme?.series.length,
    );
    expect(state.events.map((e) => e.seq)).toEqual([1, 2, 3, 4]);
  });

  it("accumulates the points of later snapshots", () => {
    let state = reduce(EMPTY_STATE, { kind: "snapshot", snapshot: simulated });
    state = reduce(state, { kind: "snapshot", snapshot: later(1) });
    const premium = state.modelSeries.premium ?? [];
    expect(premium.map((p) => p.t_ms)).toEqual([0, 3000, 4000, 5000, 6000, 7000]);
    // The second that was in progress is now final, and the new one carries its own data.
    expect(premium.at(-2)?.final).toBe(true);
    expect(premium.at(-1)?.arrived).toBe(7);
    // Receiving the same events again does not duplicate them.
    expect(state.events.map((e) => e.seq)).toEqual([1, 2, 3, 4]);
  });

  it("lets points age out of the window", () => {
    let state = reduce(EMPTY_STATE, { kind: "snapshot", snapshot: simulated });
    // 898 s later the window (900 s) starts at 4782 ms: the points at 0, 3000 and 4000 go.
    state = reduce(state, { kind: "snapshot", snapshot: later(898) });
    expect(state.modelSeries.premium?.map((p) => p.t_ms)).toEqual([5000, 6000, 904_000]);
  });

  it("handles a gateway with no applications", () => {
    const state = reduce(EMPTY_STATE, { kind: "snapshot", snapshot: passthrough });
    expect(state.applicationSeries).toEqual({});
    expect(Object.keys(state.modelSeries)).toEqual(["economy", "standard", "premium"]);
  });

  it("starts again when the gateway has restarted", () => {
    let state = reduce(EMPTY_STATE, { kind: "snapshot", snapshot: later(5) });
    state = reduce(state, { kind: "event", event: event(9) });
    state = reduce(state, { kind: "missed", missed: 3 });
    // A new process: its clock and its event numbering are behind what was seen.
    const fresh: Snapshot = {
      ...passthrough,
      events: { ...passthrough.events, last_seq: 0, recent: [] },
    };
    state = reduce(state, { kind: "snapshot", snapshot: fresh });
    expect(state.snapshot).toBe(fresh);
    expect(state.events).toEqual([]);
    expect(state.missedEvents).toBe(0);
    expect(state.modelSeries.premium?.map((p) => p.t_ms)).toEqual([0]);
  });
});

describe("isRestart", () => {
  it("is false for a snapshot that follows on", () => {
    expect(isRestart(simulated, simulated)).toBe(false);
    expect(isRestart(simulated, { ...simulated, at_ms: simulated.at_ms + 1000 })).toBe(false);
  });

  it("is true when the clock went backwards", () => {
    expect(isRestart(simulated, { ...simulated, at_ms: simulated.at_ms - 1 })).toBe(true);
  });

  it("is true when the event numbering went backwards", () => {
    const next = { ...simulated, events: { ...simulated.events, last_seq: 1 } };
    expect(isRestart(simulated, next)).toBe(true);
  });

  it("is true when a lifetime counter went down, even if the clock went on", () => {
    const premium = simulated.models.premium;
    if (premium === undefined) throw new Error("fixture has no premium model");
    const next: Snapshot = {
      ...simulated,
      at_ms: simulated.at_ms + 5000,
      models: {
        ...simulated.models,
        premium: { ...premium, traffic: { ...premium.traffic, arrived_total: 0 } },
      },
    };
    expect(isRestart(simulated, next)).toBe(true);
  });
});

describe("events", () => {
  it("appends in order and ignores a repeat", () => {
    let events = addEvent([], event(1));
    events = addEvent(events, event(2));
    expect(addEvent(events, event(2))).toBe(events);
    expect(events.map((e) => e.seq)).toEqual([1, 2]);
  });

  it("puts a late event in its place", () => {
    const events = addEvent([event(1), event(3)], event(2));
    expect(events.map((e) => e.seq)).toEqual([1, 2, 3]);
  });

  it("holds at most MAX_EVENTS, dropping the oldest", () => {
    let events: GatewayEvent[] = [];
    for (let seq = 1; seq <= MAX_EVENTS + 5; seq++) events = addEvent(events, event(seq));
    expect(events).toHaveLength(MAX_EVENTS);
    expect(events[0]?.seq).toBe(6);
    expect(events.at(-1)?.seq).toBe(MAX_EVENTS + 5);
  });

  it("adds up what the gateway says was missed", () => {
    let state = reduce(EMPTY_STATE, { kind: "missed", missed: 2 });
    state = reduce(state, { kind: "missed", missed: 5 });
    expect(state.missedEvents).toBe(7);
  });
});
