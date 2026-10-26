// What the tabs show, worked out from the API's values. Pure functions.

import type { BatchGroup, GatewayEvent, ModelPoint, SloGroup, ApplicationSnapshot } from "./types";

export type Health = "healthy" | "degraded" | "no-slo";

/** A model with no SLO has no health to report; that is not the same as healthy. */
export function health(slo: SloGroup): Health {
  if (!slo.available || slo.degraded === null) return "no-slo";
  return slo.degraded ? "degraded" : "healthy";
}

export interface SloStanding {
  /** The p99 the SLO is judged on, over the SLO monitor's window; null if nothing finished. */
  p99_ms: number | null;
  slo_ms: number;
  window_ms: number;
  samples: number;
  /** Fewer requests in the window than the monitor needs before it judges either way. */
  tooFew: boolean;
  /** p99 as a share of the SLO, for a meter; null if nothing finished. */
  ratio: number | null;
  level: "ok" | "warning" | "exhausted";
}

/**
 * Where a model stands against its SLO, on the same p99 and window the health badge is
 * decided by. null for a model with no SLO.
 */
export function sloStanding(slo: SloGroup): SloStanding | null {
  if (!slo.available || slo.slo_p99_ms === null || slo.window_ms === null) return null;
  const p99 = slo.recent_p99_ms;
  const samples = slo.samples ?? 0;
  const ratio = p99 === null ? null : p99 / slo.slo_p99_ms;
  return {
    p99_ms: p99,
    slo_ms: slo.slo_p99_ms,
    window_ms: slo.window_ms,
    samples,
    tooFew: samples < (slo.min_samples ?? 1),
    ratio,
    level: ratio === null || ratio <= 0.8 ? "ok" : ratio <= 1 ? "warning" : "exhausted",
  };
}

/**
 * Requests per second over the last second that has ended. The second in progress would
 * understate it. null if no second has ended yet.
 */
export function currentRate(points: readonly ModelPoint[]): number | null {
  for (let i = points.length - 1; i >= 0; i--) {
    const point = points[i];
    if (point?.final) return point.req_per_s;
  }
  return null;
}

/** Seated requests as a share of the batch's seats, 0..1; null if the worker cannot say. */
export function occupancy(batch: BatchGroup): number | null {
  if (!batch.available || batch.size === null || batch.max_size === null) return null;
  return batch.max_size === 0 ? 0 : batch.size / batch.max_size;
}

export type BudgetLevel = "ok" | "warning" | "exhausted";

export interface BudgetUse {
  /** Used over budget; can pass 1, since the last request admitted may overshoot. */
  ratio: number;
  level: BudgetLevel;
}

export function budgetUse(application: ApplicationSnapshot): BudgetUse {
  const ratio =
    application.budget_tokens === 0 ? 1 : application.used_tokens / application.budget_tokens;
  return { ratio, level: ratio >= 1 ? "exhausted" : ratio >= 0.8 ? "warning" : "ok" };
}

export interface EventFilter {
  type: string;
  model: string;
  application: string;
}

/** "" in a field means any. */
export const ANY_EVENT: EventFilter = { type: "", model: "", application: "" };

/** The events that match, newest first. */
export function filterEvents(events: readonly GatewayEvent[], filter: EventFilter): GatewayEvent[] {
  return events
    .filter(
      (event) =>
        (filter.type === "" || event.type === filter.type) &&
        (filter.model === "" || event.model === filter.model) &&
        (filter.application === "" || event.application_id === filter.application),
    )
    .reverse();
}

/** The distinct values of one field among the events, sorted, for a filter's choices. */
export function distinct(
  events: readonly GatewayEvent[],
  field: "type" | "model" | "application_id",
): string[] {
  const values = new Set<string>();
  for (const event of events) {
    const value = event[field];
    if (value !== null) values.add(value);
  }
  return [...values].sort();
}

const TITLES: Record<string, string> = {
  state_change: "State change",
  queue_full: "Queue full",
  timeout: "Timeout",
  downgrade: "Downgrade",
  budget_exhausted: "Budget exhausted",
  circuit_breaker: "Circuit breaker",
};

export function eventTitle(type: string): string {
  return TITLES[type] ?? type;
}

function number(value: unknown): string {
  return typeof value === "number"
    ? value.toLocaleString("en-US", { maximumFractionDigits: value >= 100 ? 0 : 1 })
    : "?";
}

/** One sentence saying what happened, from the event's own data. */
export function describeEvent(event: GatewayEvent): string {
  const model = event.model ?? "a model";
  const { data } = event;
  switch (event.type) {
    case "state_change":
      if (data.degraded === true) {
        return `${model} is degraded: recent p99 ${number(data.p99_ms)} ms is over its SLO.`;
      }
      return data.p99_ms === null
        ? `${model} recovered: nothing finished in its SLO window.`
        : `${model} recovered: recent p99 ${number(data.p99_ms)} ms.`;
    case "queue_full":
      return `${model} refused a request: ${number(data.waiting)} waiting, limit ${number(data.limit)}.`;
    case "timeout":
      return `A request to ${model} was cancelled after ${number(data.timeout_ms)} ms.`;
    case "downgrade":
      return `A request for ${model} was served by ${typeof data.to_model === "string" ? data.to_model : "a cheaper model"}.`;
    case "budget_exhausted":
      return `Refused: ${number(data.used_tokens)} of ${number(data.budget_tokens)} budget tokens used.`;
    case "circuit_breaker": {
      const provider = typeof data.provider === "string" ? data.provider : "A provider";
      if (data.state === "open") {
        return `${provider} is skipped for ${number(Number(data.open_ms) / 1000)} s after repeated failures.`;
      }
      return data.state === "half_open"
        ? `${provider} is being tried again with one request.`
        : `${provider} is answering again.`;
    }
    default:
      return JSON.stringify(data);
  }
}

/** How long before `nowMs` something happened, both on the gateway's clock: "12 s ago". */
export function ago(atMs: number, nowMs: number): string {
  const seconds = Math.max(0, Math.round((nowMs - atMs) / 1000));
  if (seconds < 60) return `${seconds} s ago`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} min ${seconds % 60} s ago`;
}
