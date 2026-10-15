import { currentRate, health, occupancy, type Health } from "../api/derive";
import type { ModelPoint, ModelSnapshot } from "../api/types";
import { formatValue } from "../charts/transforms";
import { Meter } from "./Meter";
import { Unavailable } from "./Unavailable";

const HEALTH: Record<Health, { icon: string; label: string }> = {
  healthy: { icon: "✓", label: "Healthy" },
  degraded: { icon: "!", label: "Degraded" },
  "no-slo": { icon: "–", label: "No SLO" },
};

function Stat({ label, value, unit }: { label: string; value: string; unit?: string }) {
  return (
    <div className="stat">
      <span className="stat-label">{label}</span>
      <span className="stat-value">
        {value}
        {unit !== undefined && <span className="unit"> {unit}</span>}
      </span>
    </div>
  );
}

export interface ModelCardProps {
  name: string;
  model: ModelSnapshot;
  /** The model's accumulated series, oldest first. */
  points: readonly ModelPoint[];
}

/** One model at a glance: health, load, queue, batch, and latency against its SLO. */
export function ModelCard({ name, model, points }: ModelCardProps) {
  const state = health(model.slo);
  const rate = currentRate(points);
  const seats = occupancy(model.batch);
  const total = model.latency.total_ms;
  const ttft = model.latency.ttft_ms;
  const slo = model.slo.slo_p99_ms;

  return (
    <article className="card" aria-label={name}>
      <header className="card-head">
        <h3>{name}</h3>
        <span className={`badge badge-${state}`}>
          <span aria-hidden="true">{HEALTH[state].icon}</span> {HEALTH[state].label}
        </span>
      </header>

      <div className="stats">
        <Stat label="Requests" value={formatValue(rate)} unit="req/s" />
        <Stat label="In flight" value={formatValue(model.traffic.in_flight)} />
      </div>

      {model.queue.available ? (
        <div className="stats">
          <Stat label="Queue depth" value={formatValue(model.queue.length)} unit="waiting" />
        </div>
      ) : (
        <Unavailable what="Queue depth" />
      )}

      {seats !== null ? (
        <div className="stat">
          <span className="stat-label">Batch occupancy</span>
          <span className="stat-value">
            {model.batch.size} of {model.batch.max_size}
            <span className="unit"> seats</span>
          </span>
          <Meter label={`${name} batch occupancy`} ratio={seats} />
        </div>
      ) : (
        <Unavailable what="Batch occupancy" />
      )}

      <div className="stats">
        <div className="stat">
          <span className="stat-label">p99 total latency</span>
          <span className="stat-value">
            {formatValue(total?.p99 ?? null)}
            <span className="unit"> ms</span>
          </span>
          <span className="muted">
            {slo === null ? "No SLO for this model" : `SLO ${formatValue(slo)} ms`}
          </span>
          {slo !== null && total !== null && (
            <Meter
              label={`${name} p99 total latency against its SLO`}
              ratio={total.p99 / slo}
              level={total.p99 > slo ? "exhausted" : total.p99 > slo * 0.8 ? "warning" : "ok"}
            />
          )}
        </div>
        <Stat label="p99 TTFT" value={formatValue(ttft?.p99 ?? null)} unit="ms" />
      </div>
    </article>
  );
}
