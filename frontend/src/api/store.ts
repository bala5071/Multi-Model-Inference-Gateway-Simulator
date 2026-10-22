// What the dashboard remembers between pushes: the newest snapshot, the series it has
// accumulated from all of them, and the events it has seen.

import type { GatewayEvent, ModelPoint, Snapshot, ApplicationPoint } from "./types";

export const MAX_EVENTS = 1000;

export interface MonitorState {
  snapshot: Snapshot | null;
  modelSeries: Record<string, ModelPoint[]>;
  applicationSeries: Record<string, ApplicationPoint[]>;
  /** Oldest first. */
  events: GatewayEvent[];
  /** Events the gateway said it had already dropped before this client could read them. */
  missedEvents: number;
}

export const EMPTY_STATE: MonitorState = {
  snapshot: null,
  modelSeries: {},
  applicationSeries: {},
  events: [],
  missedEvents: 0,
};

export type MonitorAction =
  | { kind: "snapshot"; snapshot: Snapshot }
  | { kind: "event"; event: GatewayEvent }
  | { kind: "missed"; missed: number };

/**
 * Fold newly pushed points into the ones already held, oldest first.
 *
 * A point for a second already held replaces it: the stream sends each second twice, once
 * while it is running and once final. Points at or before `oldestMs` are dropped.
 */
export function mergePoints<T extends { t_ms: number }>(
  existing: readonly T[],
  incoming: readonly T[],
  oldestMs: number,
): T[] {
  const byTime = new Map<number, T>();
  for (const point of existing) byTime.set(point.t_ms, point);
  for (const point of incoming) byTime.set(point.t_ms, point);
  return [...byTime.values()].filter((p) => p.t_ms > oldestMs).sort((a, b) => a.t_ms - b.t_ms);
}

/**
 * True if `next` cannot follow `previous` from the same gateway process: its clock went
 * backwards, or a counter that only counts up went down. What was accumulated is then
 * from another process and must be thrown away.
 */
export function isRestart(previous: Snapshot, next: Snapshot): boolean {
  if (next.at_ms < previous.at_ms) return true;
  if (next.events.last_seq < previous.events.last_seq) return true;
  return Object.entries(next.models).some(([name, model]) => {
    const before = previous.models[name];
    return before !== undefined && model.traffic.arrived_total < before.traffic.arrived_total;
  });
}

function applySnapshot(state: MonitorState, snapshot: Snapshot): MonitorState {
  const base = state.snapshot !== null && isRestart(state.snapshot, snapshot) ? EMPTY_STATE : state;
  const oldestMs = snapshot.at_ms - snapshot.window_ms;

  const modelSeries: Record<string, ModelPoint[]> = {};
  for (const [name, model] of Object.entries(snapshot.models)) {
    modelSeries[name] = mergePoints(base.modelSeries[name] ?? [], model.series, oldestMs);
  }
  const applicationSeries: Record<string, ApplicationPoint[]> = {};
  for (const [id, application] of Object.entries(snapshot.applications.applications ?? {})) {
    applicationSeries[id] = mergePoints(
      base.applicationSeries[id] ?? [],
      application.series,
      oldestMs,
    );
  }
  let events = base.events;
  for (const event of snapshot.events.recent) events = addEvent(events, event);
  return { ...base, snapshot, modelSeries, applicationSeries, events };
}

/** Add an event, keeping them in order, without duplicates, and at most MAX_EVENTS. */
export function addEvent(events: GatewayEvent[], event: GatewayEvent): GatewayEvent[] {
  const last = events.at(-1);
  if (last !== undefined && event.seq <= last.seq) {
    if (events.some((e) => e.seq === event.seq)) return events;
    return [...events, event].sort((a, b) => a.seq - b.seq).slice(-MAX_EVENTS);
  }
  return [...events, event].slice(-MAX_EVENTS);
}

export function reduce(state: MonitorState, action: MonitorAction): MonitorState {
  switch (action.kind) {
    case "snapshot":
      return applySnapshot(state, action.snapshot);
    case "event":
      return { ...state, events: addEvent(state.events, action.event) };
    case "missed":
      return { ...state, missedEvents: state.missedEvents + action.missed };
  }
}
