// The monitoring API, mirrored by hand from docs/15-monitoring-api.md.
//
// `src/test/fixtures/*.json` are snapshots produced by the gateway itself (a pytest keeps
// them current), and `fixtures.test.ts` assigns them to these types. If the API changes,
// the type check fails there.
//
// A group with `available: false` has every other field null. Null is "the gateway cannot
// observe this", never zero.

export interface TrafficGroup {
  available: boolean;
  in_flight: number;
  arrived_total: number;
  completed_total: number;
  output_tokens_total: number;
  queue_full_total: number;
  timeout_total: number;
  downgrade_total: number;
}

export interface QueueGroup {
  available: boolean;
  length: number | null;
}

export interface BatchGroup {
  available: boolean;
  size: number | null;
  max_size: number | null;
}

export interface KvGroup {
  available: boolean;
  used_tokens: number | null;
  capacity_tokens: number | null;
}

export interface CacheGroup {
  available: boolean;
  size_tokens: number | null;
  lookups_total: number | null;
  hits_total: number | null;
  prompt_tokens_total: number | null;
  cached_tokens_total: number | null;
  hit_rate: number | null;
  token_hit_rate: number | null;
}

export interface Quantiles {
  p50: number;
  p99: number;
  samples: number;
}

export interface LatencyGroup {
  available: boolean;
  window_ms: number;
  ttft_ms: Quantiles | null;
  itl_ms: Quantiles | null;
  total_ms: Quantiles | null;
}

export interface SloGroup {
  available: boolean;
  degraded: boolean | null;
  slo_p99_ms: number | null;
  /** Over the SLO monitor's own window, `window_ms`: not the latency group's window. */
  recent_p99_ms: number | null;
  samples: number | null;
  window_ms: number | null;
  min_samples: number | null;
}

/** One model over one second starting at `t_ms`. */
export interface ModelPoint {
  t_ms: number;
  final: boolean;
  arrived: number;
  completed: number;
  req_per_s: number;
  output_tokens: number;
  tokens_per_s: number;
  queue_full: number;
  timeouts: number;
  downgrades: number;
  in_flight: number;
  in_flight_max: number;
  queue_len: number | null;
  queue_len_max: number | null;
  batch_size: number | null;
  batch_size_max: number | null;
  kv_used_tokens: number | null;
  kv_used_tokens_max: number | null;
  cache_lookups: number | null;
  cache_hit_rate: number | null;
  cache_token_hit_rate: number | null;
  ttft_p50_ms: number | null;
  ttft_p99_ms: number | null;
  itl_p50_ms: number | null;
  itl_p99_ms: number | null;
  total_p50_ms: number | null;
  total_p99_ms: number | null;
  latency_truncated: boolean;
  degraded: boolean | null;
}

export interface ModelSnapshot {
  traffic: TrafficGroup;
  queue: QueueGroup;
  batch: BatchGroup;
  kv: KvGroup;
  cache: CacheGroup;
  latency: LatencyGroup;
  slo: SloGroup;
  series: ModelPoint[];
}

export interface ApplicationPoint {
  t_ms: number;
  final: boolean;
  requests: number;
  billed_tokens: number;
  spend_usd: number;
  rejections: number;
  timeouts: number;
  downgrades: number;
}

export interface ApplicationSnapshot {
  plan: string;
  period_start_ms: number;
  period_end_ms: number;
  budget_tokens: number;
  used_tokens: number;
  remaining_tokens: number;
  spend_usd: number;
  requests: number;
  /** Requests refused with 429, by reason, since the gateway started. */
  rejections_total: { budget_exhausted: number; queue_full: number; rate_limited: number };
  timeout_total: number;
  downgrade_total: number;
  series: ApplicationPoint[];
}

export interface ApplicationsGroup {
  available: boolean;
  applications: Record<string, ApplicationSnapshot> | null;
}

export const EVENT_TYPES = [
  "state_change",
  "queue_full",
  "timeout",
  "downgrade",
  "budget_exhausted",
] as const;

export type EventType = (typeof EVENT_TYPES)[number];

export interface GatewayEvent {
  seq: number;
  at_ms: number;
  // A string, not EventType: a newer gateway may publish types this build does not know.
  type: string;
  model: string | null;
  application_id: string | null;
  data: Record<string, unknown>;
}

export interface EventsGroup {
  available: boolean;
  capacity: number;
  retained: number;
  last_seq: number;
  counts: Record<string, number>;
  recent: GatewayEvent[];
}

export interface Snapshot {
  at_ms: number;
  resolution_ms: number;
  window_ms: number;
  models: Record<string, ModelSnapshot>;
  applications: ApplicationsGroup;
  events: EventsGroup;
}
