// Pure transforms from the gateway's alert answers (docs/44 to docs/48) to what the console's
// alert screens show (C2, docs/49). No rule about alerts lives here: what is valid, what fires,
// who may change what and where a notification goes are the gateway's answers, shown as they
// come. Nothing here ever reads a webhook's URL or a signing key: the gateway sends neither,
// except a new key once, which the channel screen shows and forgets.

import type { WriteProbe } from "./api";
import { formatTime } from "./derive";
import type {
  AlertRule,
  Channel,
  ChannelName,
  Delivery,
  Health,
  Incident,
  IncidentDetail,
  Json,
  PreviewMinute,
  Recipient,
  RuleBody,
  RuleValidation,
} from "./types";

const MINUTE_MS = 60_000;

// --- The evaluator banner -------------------------------------------------------------------

export interface Banner {
  state: string;
  text: string;
}

/**
 * The banner every alert page shows while the gateway says alerts may not be evaluated:
 * `/healthz`'s `alert_evaluator` is `stale` or `unknown`. An answer that cannot be read is
 * treated as unknown (shown, never hidden). `ok` and `disabled` show nothing. `/healthz`
 * carries the word only, no time, so the banner says no time.
 */
export function evaluatorBanner(health: Health | null, failed: boolean): Banner | null {
  if (failed) {
    return {
      state: "unreadable",
      text: "The alert evaluator's state could not be read from the gateway (/healthz). Treat alerts as not evaluated until it can.",
    };
  }
  if (health === null) return null;
  const state = health.alert_evaluator;
  if (state === "ok" || state === "disabled") return null;
  if (state === "stale") {
    return {
      state,
      text: "Alerts are not being evaluated: no evaluation cycle has completed recently (stale). Nothing fires or resolves until the evaluator recovers.",
    };
  }
  if (state === "unknown") {
    return {
      state,
      text: "This gateway cannot tell whether alerts are being evaluated (unknown): it cannot read the evaluator's heartbeat. Treat alerts as not evaluated.",
    };
  }
  return {
    state: "unreadable",
    text: "The gateway gave an alert evaluator state the console does not know. Treat alerts as not evaluated.",
  };
}

// --- Rules ------------------------------------------------------------------------------------

/** The fields of a rule answer that are not the rule's body. */
const RULE_META = new Set([
  "id",
  "version",
  "default_key",
  "changed_from_default",
  "created_at_ms",
  "updated_at_ms",
]);

/** A rule's body, as the gateway takes it back: its answer without the id and markers. */
export function ruleBody(rule: AlertRule): RuleBody {
  const body: RuleBody = {};
  for (const [field, value] of Object.entries(rule) as [string, Json][]) {
    if (!RULE_META.has(field)) body[field] = value;
  }
  return body;
}

/** A rule's condition, as words: `rejections (rate_limited) > 0 over 5 min, for 0 min`. */
export function formatCondition(rule: AlertRule): string {
  const signal = rule.parameter === null ? rule.signal : `${rule.signal} (${rule.parameter})`;
  const window = rule.window === null ? "" : ` over ${rule.window} min`;
  return `${signal} ${rule.comparison} ${rule.threshold}${window}, for ${rule.for_minutes} min`;
}

/** "default", "default, changed", or nothing: the gateway's markers. */
export function defaultMark(rule: AlertRule): string {
  if (rule.default_key === null) return "";
  return rule.changed_from_default ? "default, changed" : "default";
}

/** A rule's channels by name: the gateway's ids named from its own list. */
export function channelList(ids: readonly string[], names: readonly ChannelName[]): string {
  if (ids.length === 0) return "none";
  return ids
    .map((id) => {
      if (id === "organization defaults") return "organization defaults";
      return names.find((c) => c.id === id)?.name ?? id;
    })
    .join(", ");
}

/** The last complete minute's value, as the preview gave it: a number or why there is none. */
export function lastValue(
  preview: RuleValidation["preview"] | undefined,
  incident: Incident | undefined,
): string {
  const minutes = preview?.minutes ?? [];
  const last = minutes[minutes.length - 1];
  if (last !== undefined) {
    if (last.value !== null) return formatNumber(last.value);
    return last.why_not === "too_few" ? "too few to judge" : (last.why_not ?? "no value");
  }
  if (incident?.value !== undefined && incident.value !== null) {
    return `${formatNumber(incident.value)} (when it opened)`;
  }
  return "not previewed";
}

function formatNumber(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(2);
}

/** An incident still open, in the gateway's words: anything not `resolved`. */
export function isOpen(incident: Incident): boolean {
  return incident.state !== "resolved";
}

export interface RuleRow {
  id: string;
  name: string;
  mark: string;
  condition: string;
  severity: string;
  channels: string;
  enabled: boolean;
  state: string;
  last: string;
}

/** Each rule with its state (its open incident's, or whether it is on) and last value. */
export function ruleRows(
  rules: readonly AlertRule[],
  previews: Readonly<Record<string, RuleValidation | null>>,
  incidents: readonly Incident[],
  names: readonly ChannelName[],
): RuleRow[] {
  return rules.map((rule) => {
    const open = incidents.find((i) => i.rule_id === rule.id && isOpen(i));
    return {
      id: rule.id,
      name: rule.name,
      mark: defaultMark(rule),
      condition: formatCondition(rule),
      severity: rule.severity,
      channels: channelList(rule.channels, names),
      enabled: rule.enabled,
      state: open !== undefined ? open.state : rule.enabled ? "not firing" : "disabled",
      last: lastValue(previews[rule.id]?.preview, open),
    };
  });
}

/** The rule editor: each body field as the JSON text typed. */
export type RuleDraft = Record<string, string>;

export function initialRuleDraft(body: RuleBody): RuleDraft {
  const draft: RuleDraft = {};
  for (const [field, value] of Object.entries(body)) draft[field] = JSON.stringify(value);
  return draft;
}

/** The body a draft asks for, and the fields whose text is not JSON at all. */
export function draftRule(draft: RuleDraft): { body: RuleBody; unparsed: string[] } {
  const body: RuleBody = {};
  const unparsed: string[] = [];
  for (const [field, text] of Object.entries(draft)) {
    try {
      body[field] = JSON.parse(text) as Json;
    } catch {
      unparsed.push(field);
    }
  }
  return { body, unparsed };
}

/** The clock time of a minute since the epoch: `10:14 UTC`. */
export function formatMinute(minute: number): string {
  return `${new Date(minute * MINUTE_MS).toISOString().slice(11, 16)} UTC`;
}

export interface PreviewSummary {
  /** Each stretch of consecutive minutes in which the condition held. */
  runs: string[];
  tooFew: number;
  total: number;
}

/**
 * What `validate` said about the last hour, in words: when the condition held (each run of
 * minutes the gateway marked `met`) and how many minutes had too few requests to judge. It
 * says when the condition held, not when the rule would have fired: firing also depends on
 * `for`, cooldown and the incident's state, which the evaluator decides.
 */
export function previewSummary(minutes: readonly PreviewMinute[]): PreviewSummary {
  const runs: string[] = [];
  let start: number | null = null;
  let previous: number | null = null;
  const close = () => {
    if (start !== null && previous !== null) {
      const count = previous - start + 1;
      runs.push(
        `${formatMinute(start)} to ${formatMinute(previous + 1)} (${count} minute${count === 1 ? "" : "s"})`,
      );
    }
  };
  for (const m of minutes) {
    if (m.met && start !== null && previous !== null && m.minute === previous + 1) {
      previous = m.minute;
    } else if (m.met) {
      close();
      start = m.minute;
      previous = m.minute;
    } else {
      close();
      start = null;
      previous = null;
    }
  }
  close();
  return {
    runs,
    tooFew: minutes.filter((m) => m.why_not === "too_few").length,
    total: minutes.length,
  };
}

// --- Incidents ---------------------------------------------------------------------------------

export interface IncidentRow {
  id: string;
  applicationId: string | null;
  application: string;
  rule: string;
  state: string;
  severity: string;
  value: string;
  opened: string;
  resolved: string;
}

/** A time, or a dash when the gateway gave none. */
export function maybeTime(ms: number | null): string {
  return ms === null ? "-" : formatTime(ms);
}

/**
 * Each incident with its rule's name (from the rules of its application the console could
 * read), its state and times. An incident with no application is the built-in rule's for a
 * channel (docs/46).
 */
export function incidentRows(
  incidents: readonly Incident[],
  ruleNames: Readonly<Record<string, Readonly<Record<string, string>>>>,
): IncidentRow[] {
  return incidents.map((i) => ({
    id: i.id,
    applicationId: i.application_id,
    application: i.application_id ?? "the organization (built-in)",
    rule:
      i.application_id === null
        ? "channel delivery failing (built-in)"
        : (ruleNames[i.application_id]?.[i.rule_id] ?? i.rule_id),
    state: i.state,
    severity: i.severity,
    value: i.value === null ? "-" : formatNumber(i.value),
    opened: formatTime(i.opened_at_ms),
    resolved: i.resolved_at_ms === null ? "-" : formatTime(i.resolved_at_ms),
  }));
}

/** An incident's timeline: each moment the gateway recorded, in order. */
export function incidentTimeline(incident: Incident): [string, string][] {
  const moments: [number, string][] = [];
  moments.push([incident.opened_at_ms, "Opened"]);
  if (incident.pending_since_ms !== null && incident.pending_since_ms !== incident.opened_at_ms) {
    moments.push([incident.pending_since_ms, "Pending"]);
  }
  if (incident.fired_at_ms !== null) {
    moments.push([incident.fired_at_ms, incident.reopened ? "Fired (reopened)" : "Fired"]);
  }
  if (incident.acknowledged_at_ms !== null) {
    moments.push([
      incident.acknowledged_at_ms,
      `Acknowledged${incident.acknowledged_by === null ? "" : ` by ${incident.acknowledged_by}`}`,
    ]);
  }
  if (incident.no_data_since_ms !== null) moments.push([incident.no_data_since_ms, "No data"]);
  if (incident.resolved_at_ms !== null) {
    moments.push([
      incident.resolved_at_ms,
      `Resolved${incident.resolve_reason === null ? "" : ` (${incident.resolve_reason})`}`,
    ]);
  }
  return moments.sort((a, b) => a[0] - b[0]).map(([at, what]) => [formatTime(at), what]);
}

export interface NotificationRow {
  kind: string;
  state: string;
  at: string;
  reason: string;
  deliveries: string;
}

/** Each notification of an incident, and where each went and how it ended, by channel name. */
export function notificationRows(
  detail: IncidentDetail,
  names: readonly ChannelName[],
): NotificationRow[] {
  return detail.notifications.map((n) => ({
    kind: n.kind,
    state: n.state,
    at: formatTime(n.at_ms),
    reason: n.reason ?? "-",
    deliveries:
      n.deliveries.length === 0
        ? "none"
        : n.deliveries
            .map((d) => {
              const name = names.find((c) => c.id === d.channel_id)?.name ?? d.channel_id;
              const code = d.last_code === null ? "" : `, ${d.last_code}`;
              return `${name}: ${d.state} (${d.attempts} attempt${d.attempts === 1 ? "" : "s"}${code})`;
            })
            .join("; "),
  }));
}

// --- Channels ----------------------------------------------------------------------------------

export interface ChannelRow {
  id: string;
  name: string;
  type: string;
  enabled: string;
  isDefault: string;
  target: string;
  lastDelivered: string;
  lastFailed: string;
  failures: number;
}

/**
 * A channel's target as the console shows it: a webhook's origin (the gateway sends nothing
 * more), or an email channel's recipients counted by state. Never a URL.
 */
export function channelTarget(channel: Channel): string {
  if (channel.type === "webhook") return channel.url_origin ?? "-";
  const recipients = channel.recipients ?? [];
  const verified = recipients.filter((r) => r.state === "verified").length;
  return `${recipients.length} recipient${recipients.length === 1 ? "" : "s"}, ${verified} verified`;
}

export function channelRows(channels: readonly Channel[]): ChannelRow[] {
  return channels.map((c) => ({
    id: c.id,
    name: c.name,
    type: c.type,
    enabled: c.enabled ? "yes" : "no",
    isDefault: c.default ? "yes" : "no",
    target: channelTarget(c),
    lastDelivered: maybeTime(c.health.last_delivered_at_ms),
    lastFailed: maybeTime(c.health.last_failed_at_ms),
    failures: c.health.consecutive_failures,
  }));
}

export interface RecipientRow {
  id: string;
  address: string;
  state: string;
  added: string;
  verified: string;
  expires: string;
  unverified: boolean;
}

export function recipientRows(recipients: readonly Recipient[]): RecipientRow[] {
  return recipients.map((r) => ({
    id: r.id,
    address: r.address,
    state: r.state,
    added: formatTime(r.added_at_ms),
    verified: maybeTime(r.verified_at_ms),
    expires: maybeTime(r.confirmation_expires_at_ms),
    unverified: r.state !== "verified",
  }));
}

export interface DeliveryRow {
  id: string;
  notification: string;
  state: string;
  attempts: number;
  lastCode: string;
  next: string;
  updated: string;
}

export function deliveryRows(deliveries: readonly Delivery[]): DeliveryRow[] {
  return deliveries.map((d) => ({
    id: d.id,
    notification: d.notification_id,
    state: d.state,
    attempts: d.attempts,
    lastCode: d.last_code ?? "-",
    next: maybeTime(d.next_attempt_at_ms),
    updated: formatTime(d.updated_at_ms),
  }));
}

/** The recipients typed, one address a line, blank lines dropped; the gateway checks each. */
export function addressLines(text: string): string[] {
  return text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line !== "");
}

// --- Who may (note 49) -------------------------------------------------------------------------

/**
 * A control shown on the answer of an action with the same role row, for an action with no dry
 * run (note 49; the rows are pinned equal by tests/test_console_probes.py): Acknowledge on the
 * rule probe of the same application; channel writes, Disable, Delete, Remove and Test send on
 * the channel list's own answer.
 */
export function followsProbe(probe: WriteProbe | null): boolean {
  return probe === "allowed";
}
