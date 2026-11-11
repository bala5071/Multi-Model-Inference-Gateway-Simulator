// The admin endpoints the console reads and writes, mirrored by hand from the gateway's
// answers (docs/27, docs/40, docs/42). The gateway decides everything; these are only shapes.

export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };

export interface SessionUser {
  user_id: string;
  username: string;
  organization_id: string;
  must_change_password: boolean;
}

/** `POST /v1/auth/login`. */
export interface LoginAnswer {
  user: SessionUser;
  csrf_token: string;
}

/** `GET /v1/auth/me`. */
export interface MeAnswer extends LoginAnswer {
  step_up_until_ms: number | null;
}

/** One setting of an application, as `GET .../applications/{app}/settings` explains it. */
export interface EffectiveSetting {
  value: Json;
  source: string;
  capped_by: string | null;
  stored: Json;
  caps: Record<string, Json>;
}

export interface ApplicationSettings {
  application_id: string;
  organization_id: string;
  mode: { organization: string; application: string; effective: string };
  version: number;
  settings: Record<string, Json>;
  effective: Record<string, EffectiveSetting>;
  ignored_aliases: string[];
}

export interface OrganizationSettings {
  organization_id: string;
  mode: string;
  version: number;
  settings: Record<string, Json>;
  platform_caps: Record<string, Record<string, Json>>;
}

/** `POST .../settings/validate`: the dry run of a change. */
export interface Validation {
  valid: boolean;
  code: string | null;
  message: string | null;
  invalid_setting: string | null;
  refusals: { setting: string; level: string }[];
  changed: string[];
  settings: Record<string, Json>;
  loosens: string[];
  step_up_required: boolean;
  effective?: Record<string, Json>;
}

/** A stored change: `PUT`, revert and a preset. */
export interface Change {
  version: number;
  changed: string[];
  settings: Record<string, Json>;
}

export interface VersionSummary {
  version: number;
  changed_fields: string[];
  changed_by_user_id: string | null;
  via: string;
  changed_at_ms: number;
}

export interface VersionPage {
  versions: VersionSummary[];
  next_after: number | null;
}

export interface ApplicationSummary {
  application_id: string;
  name: string;
  organization_id: string;
  plan: string;
  allowed_models: string[];
  max_tokens: number | null;
  requests_per_minute: number | null;
  created_at_ms: number;
  /** The mode in force for it: the stricter of its organization's and its own (docs/51). */
  mode: string;
}

export interface ApplicationPage {
  applications: ApplicationSummary[];
  next: string | null;
}

export interface PresetList {
  presets: Record<string, Record<string, Json>>;
}

/** The body of every refusal: `{"error": {"code", "message"}}`. */
export interface ErrorBody {
  error: { code: string; message: string };
}

// --- Alerts (C2): docs/44, docs/45, docs/46, docs/48. Shapes only; the gateway decides. ---

/** `GET /healthz`: `alert_evaluator` is one word (docs/45). */
export interface Health {
  alert_evaluator?: string;
}

/** A rule's body, as the gateway shows it and takes it back (docs/44). */
export type RuleBody = Record<string, Json>;

/** `GET .../alert-rules`: each rule's body beside its id, version and default markers. */
export interface AlertRule {
  id: string;
  version: number;
  default_key: string | null;
  changed_from_default: boolean;
  name: string;
  signal: string;
  parameter: string | null;
  comparison: string;
  threshold: number;
  window: number | null;
  for_minutes: number;
  severity: string;
  channels: string[];
  cooldown_minutes: number;
  repeat_every_minutes: number | null;
  quiet_hours: Json;
  enabled: boolean;
  created_at_ms: number;
  updated_at_ms: number;
}

export interface RulePage {
  rules: AlertRule[];
  next: string | null;
}

/** One minute of the last hour, as `validate` previews it. */
export interface PreviewMinute {
  minute: number;
  value: number | null;
  why_not: string | null;
  met: boolean;
}

/** `POST .../alert-rules/validate`. */
export interface RuleValidation {
  valid: boolean;
  invalid_field: string | null;
  message: string | null;
  rule: RuleBody | null;
  preview: { minutes: PreviewMinute[]; met: number } | null;
}

/** `GET .../applications/{app}/alert-channels`: what a rule may name, never a target. */
export interface ChannelName {
  id: string;
  name: string;
  type: string;
  default: boolean;
  enabled: boolean;
}

export interface Incident {
  id: string;
  application_id: string | null;
  rule_id: string;
  rule_version: number;
  series: string | null;
  state: string;
  severity: string;
  value: number | null;
  opened_at_ms: number;
  pending_since_ms: number | null;
  fired_at_ms: number | null;
  acknowledged_at_ms: number | null;
  acknowledged_by: string | null;
  resolved_at_ms: number | null;
  resolve_reason: string | null;
  no_data_since_ms: number | null;
  reopened: boolean;
}

export interface IncidentPage {
  incidents: Incident[];
  next: string | null;
}

export interface NotificationDelivery {
  channel_id: string;
  state: string;
  attempts: number;
  last_code: string | null;
}

export interface IncidentNotification {
  kind: string;
  state: string;
  at_ms: number;
  reason: string | null;
  deliveries: NotificationDelivery[];
}

export interface IncidentDetail extends Incident {
  notifications: IncidentNotification[];
}

/** An email channel's recipient, for the organization's admin only (docs/48). */
export interface Recipient {
  id: string;
  address: string;
  state: string;
  added_at_ms: number;
  verified_at_ms: number | null;
  confirmation_expires_at_ms: number | null;
}

/** A channel, for the organization's admin: a webhook's origin only, never its URL or key. */
export interface Channel {
  id: string;
  name: string;
  type: string;
  enabled: boolean;
  default: boolean;
  url_origin: string | null;
  version: number;
  created_at_ms: number;
  updated_at_ms: number;
  health: {
    last_delivered_at_ms: number | null;
    last_failed_at_ms: number | null;
    consecutive_failures: number;
  };
  recipients?: Recipient[];
}

export interface ChannelPage {
  channels: Channel[];
  next: string | null;
}

/** `POST .../alert-channels`: a webhook's signing key, this once; an email channel's confirmations. */
export interface CreatedChannel extends Channel {
  signing_key?: string;
  confirmations?: Record<string, string>;
}

export interface Delivery {
  id: string;
  notification_id: string;
  channel_id: string;
  state: string;
  attempts: number;
  next_attempt_at_ms: number | null;
  last_code: string | null;
  created_at_ms: number;
  updated_at_ms: number;
}

export interface DeliveryPage {
  deliveries: Delivery[];
  next: string | null;
}

export interface TestSend {
  id: string;
  outcome: string;
  delivered: boolean;
}

// --- What a session may do (P2): GET /v1/auth/capabilities, docs/50. ---

/** One of the session's own roles, as the gateway names it: shown, never interpreted. */
export interface HeldRole {
  role: string;
  scope_type: string;
  scope_id: string;
}

/** An application the session can name, and each navigation action's answer on it. */
export interface NamedApplication {
  application_id: string;
  actions: Record<string, boolean>;
}

/**
 * `GET /v1/auth/capabilities`: each navigation action as the gateway's `authorize` answers it,
 * for the platform, the session's organization and the applications it can name. The console
 * shows only what this says; every action is still checked by its own route.
 */
export interface Capabilities {
  roles: HeldRole[];
  platform: Record<string, boolean>;
  organization: Record<string, boolean>;
  applications: NamedApplication[];
  more_applications: boolean;
}

// --- The Applications list and Add application (docs/51). Shapes only; the gateway decides. ---

/** One figure: its value, or why there is none (`too_few`, `incomplete`, `unavailable`,
 * `above_range`). Never a 0 standing in for "not known". */
export interface Figure {
  value: number | null;
  why_not: string | null;
}

export interface FigureWindow {
  minutes: number;
  first_minute_ms: number;
  end_ms: number;
  /** When this process's rollups began, if inside the window (a restart). */
  since_ms: number | null;
}

export interface ApplicationFigures {
  application_id: string;
  requests: Figure;
  error_rate: Figure;
  latency_total_p50: Figure;
}

/** `GET .../metrics`: one page of the organization's applications, the list's own paging. */
export interface FiguresPage {
  window: FigureWindow;
  applications: ApplicationFigures[];
  next: string | null;
}

export interface OpenCounts {
  application_id: string;
  pending: number;
  firing: number;
  acknowledged: number;
}

/** `GET .../incidents/counts`: the applications with an open incident only. */
export interface OpenCountsAnswer {
  applications: OpenCounts[];
}

export interface PlanTemplate {
  name: string;
  allowed_models: string[];
  monthly_token_budget: number;
  max_tokens: number | null;
  requests_per_minute: number | null;
}

/** `GET .../plans`: what an application can be created from. */
export interface PlanList {
  plans: PlanTemplate[];
}

/** An application's key as the gateway lists it: never the key, never its HMAC. */
export interface ApiKeySummary {
  key_id: string;
  label: string;
  state: string;
  created_at_ms: number;
  expires_at_ms: number | null;
  last_used_at_ms: number | null;
}

export interface KeyPage {
  keys: ApiKeySummary[];
  next: string | null;
}

/** A key just issued or rotated: the one answer that carries it, shown once. */
export interface IssuedKey extends ApiKeySummary {
  key: string;
}
