// Pure transforms from the gateway's Clients answers to what the platform owner's pages show
// (docs/57). No rule lives here: who may read a client, every figure and every count are the
// gateway's answers. A figure the gateway could not give is said in words, never drawn as 0, and
// a window whose counts begin late says so, so a 24-hour figure never looks whole when it is not.

import { applicationStatus, figureText, openText, type Status } from "./applications";
import type {
  ClientApplication,
  ClientApplicationMetrics,
  ClientSpan,
  Figure,
  OrganizationList,
  OrganizationUsage,
  PlatformTotals,
  PlatformUsage,
} from "./types";

/** The windows the Clients pages may ask for, in the switch's order, and their words. */
export const CLIENT_WINDOWS = ["60m", "24h"] as const;
export type ClientWindow = (typeof CLIENT_WINDOWS)[number];
export const CLIENT_WINDOW_WORDS: Record<ClientWindow, string> = {
  "60m": "last 60 minutes",
  "24h": "last 24 hours",
};
/** The page size of the organization list and of its figures: the same, so they agree. */
export const CLIENTS_PAGE_SIZE = 50;
export const CLIENT_BANNER = "Viewing as platform owner: metrics only";
export const PLATFORM_SETTINGS_WORDS =
  "Platform settings are environment variables, set where the gateway runs.";

const UNREAD: Figure = { value: null, why_not: "unavailable" };
const MINUTE_MS = 60_000;

function utcTime(ms: number): string {
  return new Date(ms).toISOString().slice(11, 16);
}

function utcDayTime(ms: number): string {
  const iso = new Date(ms).toISOString();
  return `${iso.slice(5, 10)} ${iso.slice(11, 16)}`;
}

/**
 * The window in words: always which one, and, when its counts begin inside it, since when and
 * why. A window that is not whole says "not a whole" so its figures are never read as complete.
 */
export function spanText(span: ClientSpan | null, window: ClientWindow): string {
  const words = CLIENT_WINDOW_WORDS[window];
  if (span === null || span.since_ms === null) return words;
  const since = window === "24h" ? utcDayTime(span.since_ms) : utcTime(span.since_ms);
  const whole = window === "24h" ? "24 hours" : "60 minutes";
  const why =
    span.since_reason === "restarted"
      ? "when the gateway restarted"
      : "when the gateway began counting them";
  return `${words}, only since ${since} UTC, ${why}: not a whole ${whole}`;
}

/** Whether the window's counts begin inside it: the page then marks every figure. */
export function partial(span: ClientSpan | null): boolean {
  return span !== null && span.since_ms !== null;
}

export interface ClientRow {
  id: string;
  name: string;
  kind: string;
  enabled: boolean;
  applications: number;
  requests: string;
  errorRate: string;
  fallbackRate: string;
  open: string;
  status: Status;
}

/**
 * One row per organization of the list's page, with its figures and open incidents beside it
 * (the usage page has the same order and cursor). `usage` and `evaluator` are null when the
 * gateway could not be read for them: the rows then say so.
 */
export function clientRows(
  list: OrganizationList,
  usage: PlatformUsage | null,
  evaluator: string | null,
): ClientRow[] {
  const usageOf = new Map<string, OrganizationUsage>(
    usage?.organizations.map((u) => [u.organization_id, u]) ?? [],
  );
  return list.organizations.map((organization) => {
    const found = usageOf.get(organization.organization_id);
    const requests = found?.requests ?? UNREAD;
    const open = found === undefined ? null : found.open_incidents;
    return {
      id: organization.organization_id,
      name: organization.name,
      kind: organization.kind,
      enabled: organization.enabled,
      applications: organization.applications,
      requests: figureText(requests, "count"),
      errorRate: figureText(found?.error_rate ?? UNREAD, "percent"),
      fallbackRate: figureText(found?.fallback_rate ?? UNREAD, "percent"),
      open: openText(open),
      status: applicationStatus(open, evaluator, requests),
    };
  });
}

export interface ApplicationMetricsRow {
  id: string;
  name: string;
  requests: string;
  errorRate: string;
  fallbackRate: string;
  p50: string;
  open: string;
  status: Status;
}

/** One row per application of a client, with its figures, open incidents and status. */
export function clientApplicationRows(
  applications: readonly ClientApplication[],
  evaluator: string | null,
): ApplicationMetricsRow[] {
  return applications.map((a) => ({
    id: a.application_id,
    name: a.name,
    requests: figureText(a.requests, "count"),
    errorRate: figureText(a.error_rate, "percent"),
    fallbackRate: figureText(a.fallback_rate, "percent"),
    p50: figureText(a.latency_total_p50, "ms"),
    open: openText(a.open_incidents),
    status: applicationStatus(a.open_incidents, evaluator, a.requests),
  }));
}

export interface BucketRow {
  key: string;
  /** When the bucket starts, in UTC: HH:MM for a minute, MM-DD HH:MM for an hour. */
  when: string;
  requests: string;
  errorRate: string;
  fallbackRate: string;
  p50: string;
}

/** One application's buckets as rows, newest first (the table a row of the client view opens). */
export function bucketRows(metrics: ClientApplicationMetrics): BucketRow[] {
  const hourly = metrics.window.bucket_ms > MINUTE_MS;
  return [...metrics.buckets].reverse().map((b) => ({
    key: b.start_ms.toString(),
    when: hourly ? utcDayTime(b.start_ms) : utcTime(b.start_ms),
    requests: figureText(b.requests, "count"),
    errorRate: figureText(b.error_rate, "percent"),
    fallbackRate: figureText(b.fallback_rate, "percent"),
    p50: figureText(b.latency_total_p50, "ms"),
  }));
}

export interface HealthItem {
  label: string;
  value: string;
  tone: "ok" | "warn" | "bad" | "info";
}

const EVALUATOR_TONES: Record<string, HealthItem["tone"]> = {
  ok: "ok",
  stale: "bad",
  unknown: "warn",
  disabled: "info",
};

/**
 * The platform's health strip: the alert evaluator's heartbeat (`/healthz`), whether the stores
 * are ready (`/readyz`), and the platform's totals over the window. Each says "unknown" when it
 * could not be read, never "ok".
 */
export function healthItems(
  evaluator: string | null,
  ready: boolean | null,
  totals: PlatformTotals | null,
): HealthItem[] {
  const items: HealthItem[] = [
    {
      label: "Alert evaluator",
      value: evaluator ?? "unknown",
      tone: evaluator === null ? "warn" : (EVALUATOR_TONES[evaluator] ?? "warn"),
    },
    {
      label: "Stores",
      value: ready === null ? "unknown" : ready ? "ready" : "not ready",
      tone: ready === null ? "warn" : ready ? "ok" : "bad",
    },
  ];
  const t = totals ?? {
    requests: UNREAD,
    error_rate: UNREAD,
    fallback_rate: UNREAD,
    open_incidents: null,
  };
  items.push(
    { label: "Requests", value: figureText(t.requests, "count"), tone: "info" },
    { label: "Error rate", value: figureText(t.error_rate, "percent"), tone: "info" },
    { label: "Fallback rate", value: figureText(t.fallback_rate, "percent"), tone: "info" },
    {
      label: "Open incidents",
      value: totals === null || t.open_incidents === null ? "unknown" : openText(t.open_incidents),
      tone: "info",
    },
  );
  return items;
}
