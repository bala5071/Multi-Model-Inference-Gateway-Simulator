// Pure transforms from the gateway's answers to what the Applications list shows (docs/51). No
// rule lives here: who may see or create what, and every figure, are the gateway's answers. A
// figure the gateway could not give is said in words, never drawn as 0.

import type {
  ApplicationPage,
  Capabilities,
  Figure,
  FiguresPage,
  FigureWindow,
  OpenCounts,
  OpenCountsAnswer,
} from "./types";

/** The window every figure on the list covers, in the words the page uses. */
export const WINDOW_WORDS = "last 60 minutes";

/**
 * What a figure too few requests stand behind is drawn as (docs/54): a dash, explained by a
 * tooltip, words for screen readers, and the table's footnote. Never 0.
 */
// An em dash, written as its code point (the character check keeps sources ASCII).
export const TOO_FEW = String.fromCodePoint(0x2014);
/** The alert signals' minimums (gateway.signals, pinned by tests/test_dashboard.py). */
export const RATE_MINIMUM = 20;
export const P50_MINIMUM = 10;
export const TOO_FEW_WORDS = "too few to judge";
export const TOO_FEW_EXPLAINED = `Too few to judge: fewer than ${RATE_MINIMUM.toString()} requests for a rate, ${P50_MINIMUM.toString()} for the p50, in the window.`;

const WHY_NOT: Record<string, string> = {
  too_few: TOO_FEW,
  incomplete: "incomplete",
  unavailable: "unavailable",
  above_range: "above the measured range",
};

/** A latency in milliseconds, as the page writes it. */
export function formatMs(ms: number): string {
  if (ms < 10) return `${ms.toFixed(1)} ms`;
  if (ms < 1000) return `${Math.round(ms).toString()} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

/** A figure in words: its value in its unit, or why there is none. */
export function figureText(figure: Figure, unit: "count" | "percent" | "ms"): string {
  if (figure.value === null) {
    return figure.why_not === null ? "not available" : (WHY_NOT[figure.why_not] ?? "not available");
  }
  if (unit === "count") return Math.round(figure.value).toString();
  if (unit === "percent") return `${figure.value.toFixed(1)} %`;
  return formatMs(figure.value);
}

/** The window as the page states it: always "last 60 minutes", and since when after a restart. */
export function windowText(window: FigureWindow | null): string {
  if (window === null) return WINDOW_WORDS;
  if (window.since_ms === null) return WINDOW_WORDS;
  const since = new Date(window.since_ms).toISOString().slice(11, 16);
  return `${WINDOW_WORDS} (since ${since} UTC, when the gateway restarted)`;
}

const MODE_WORDS: Record<string, string> = {
  local_only: "local only",
  local_with_hosted_fallback: "local, hosted fallback",
  hosted_primary_with_local_fallback: "hosted first, local fallback",
};

/** The mode in force, in words; one the page does not know is shown as the gateway named it. */
export function modeText(mode: string): string {
  return MODE_WORDS[mode] ?? mode;
}

export type StatusKind = "firing" | "attention" | "ok" | "no_requests" | "unknown";

export interface Status {
  kind: StatusKind;
  words: string;
}

const STATUS_WORDS: Record<StatusKind, string> = {
  firing: "firing",
  attention: "attention",
  ok: "ok",
  no_requests: "no requests",
  unknown: "unknown",
};

function status(kind: StatusKind): Status {
  return { kind, words: STATUS_WORDS[kind] };
}

/**
 * An application's status, from the gateway's own states and nothing else: unknown when the
 * incident counts or the evaluator's heartbeat could not be read, or the evaluator is not ok;
 * firing with a firing incident; attention with one pending or acknowledged; then no requests,
 * or ok. A request count the gateway could not give leaves it unknown.
 *
 * `counts` is the application's open counts (`undefined`: none open), or `null` when the counts
 * could not be read; `evaluator` is `/healthz`'s word, or `null` when it could not be read.
 */
export function applicationStatus(
  counts: OpenCounts | undefined | null,
  evaluator: string | null,
  requests: Figure,
): Status {
  if (counts === null || evaluator !== "ok") return status("unknown");
  if (counts !== undefined && counts.firing > 0) return status("firing");
  if (counts !== undefined && counts.pending + counts.acknowledged > 0) {
    return status("attention");
  }
  if (requests.value === null) return status("unknown");
  return status(requests.value === 0 ? "no_requests" : "ok");
}

/** Open incidents in words: how many, and in which states. */
export function openText(counts: OpenCounts | undefined | null): string {
  if (counts === null) return "unknown";
  if (counts === undefined) return "0";
  const parts = (["firing", "pending", "acknowledged"] as const)
    .filter((state) => counts[state] > 0)
    .map((state) => `${counts[state].toString()} ${state}`);
  const total = counts.firing + counts.pending + counts.acknowledged;
  return parts.length === 0 ? "0" : `${total.toString()} (${parts.join(", ")})`;
}

export interface ApplicationRow {
  id: string;
  name: string;
  mode: string;
  requests: string;
  errorRate: string;
  p50: string;
  open: string;
  status: Status;
  /** Whether the gateway named this application with its keys' list allowed. */
  keys: boolean;
}

const UNREAD: Figure = { value: null, why_not: "unavailable" };

/**
 * One row per application of the list's page, with its figures, open incidents and status.
 * `figures`, `counts` and `evaluator` are `null` when the gateway could not be read for them:
 * the rows then say so, figure by figure.
 */
export function applicationRows(
  page: ApplicationPage,
  figures: FiguresPage | null,
  counts: OpenCountsAnswer | null,
  evaluator: string | null,
  capabilities: Capabilities,
): ApplicationRow[] {
  const figuresOf = new Map(figures?.applications.map((f) => [f.application_id, f]) ?? []);
  const countsOf = new Map(counts?.applications.map((c) => [c.application_id, c]) ?? []);
  return page.applications.map((application) => {
    const id = application.application_id;
    const found = figuresOf.get(id);
    const requests = found?.requests ?? UNREAD;
    const open = counts === null ? null : countsOf.get(id);
    return {
      id,
      name: application.name,
      mode: modeText(application.mode),
      requests: figureText(requests, "count"),
      errorRate: figureText(found?.error_rate ?? UNREAD, "percent"),
      p50: figureText(found?.latency_total_p50 ?? UNREAD, "ms"),
      open: openText(open),
      status: applicationStatus(open, evaluator, requests),
      keys:
        capabilities.applications.find((a) => a.application_id === id)?.actions[
          "application_keys.list"
        ] === true,
    };
  });
}

/** What the list's search and filters ask for (docs/56): on the page shown only. */
export interface RowFilter {
  /** Words in the name or the id, any case; empty asks for every row. */
  text: string;
  /** A mode in words (`modeText`), or "" for any. */
  mode: string;
  /** A status kind, or "" for any. */
  status: StatusKind | "";
}

/** The page's rows the search and filters keep, in their order. Pure. */
export function filterRows(rows: readonly ApplicationRow[], filter: RowFilter): ApplicationRow[] {
  const words = filter.text.trim().toLowerCase();
  return rows.filter(
    (row) =>
      (words === "" ||
        row.name.toLowerCase().includes(words) ||
        row.id.toLowerCase().includes(words)) &&
      (filter.mode === "" || row.mode === filter.mode) &&
      (filter.status === "" || row.status.kind === filter.status),
  );
}

/** What the page says about the search and filters, and how much of the page they keep. */
export const FILTER_NOTE = "Search and filters apply to the applications on this page only.";
