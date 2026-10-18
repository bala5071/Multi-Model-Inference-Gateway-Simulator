import { currentRate, health, occupancy, sloStanding, type Health } from "../api/derive";
import type { ModelPoint, ModelSnapshot } from "../api/types";
import { formatValue, formatWindow } from "../charts/transforms";
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
  // The health badge is decided on the SLO monitor's p99 over its own window, so the SLO
  // line and meter use that one too; the headline p99 is over the longer metrics window.
  const standing = sloStanding(model.slo);

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
          <span className="muted">last {formatWindow(model.latency.window_ms)}</span>
        </div>
        <Stat label="p99 TTFT" value={formatValue(ttft?.p99 ?? null)} unit="ms" />
      </div>

      {standing === null ? (
        <p className="muted slo-line">No SLO for this model</p>
      ) : (
        <div className="slo-line">
          <span className="muted">
            SLO {formatValue(standing.slo_ms)} ms, judged on the p99 of the last{" "}
            {formatWindow(standing.window_ms)}:{" "}
            {standing.p99_ms === null
              ? "no request finished"
              : `${formatValue(standing.p99_ms)} ms over ${standing.samples} ${
                  standing.samples === 1 ? "request" : "requests"
                }${standing.tooFew ? ", too few to judge" : ""}`}
          </span>
          {standing.ratio !== null && (
            <Meter
              label={`${name} p99 of the last ${formatWindow(standing.window_ms)} against its SLO`}
              ratio={standing.ratio}
              level={standing.level}
            />
          )}
        </div>
      )}
    </article>
  );
}
