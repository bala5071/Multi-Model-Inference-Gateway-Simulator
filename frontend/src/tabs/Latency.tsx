import type { MonitorState } from "../api/store";
import type { ModelPoint } from "../api/types";
import { ChartPanel, type PanelLine } from "../charts/ChartPanel";
import { formatValue, toColumns } from "../charts/transforms";

// When several seconds share one drawn point the largest is kept: a latency chart is
// read for its spikes.
const LINES: PanelLine[] = [
  { label: "p50", slot: 1, reducer: "max" },
  { label: "p99", slot: 2, reducer: "max" },
];

interface Metric {
  title: string;
  p50: (point: ModelPoint) => number | null;
  p99: (point: ModelPoint) => number | null;
  hasSlo: boolean;
}

const METRICS: Metric[] = [
  {
    title: "Time to first token",
    p50: (p) => p.ttft_p50_ms,
    p99: (p) => p.ttft_p99_ms,
    hasSlo: false,
  },
  {
    title: "Inter-token latency",
    p50: (p) => p.itl_p50_ms,
    p99: (p) => p.itl_p99_ms,
    hasSlo: false,
  },
  // The gateway's SLO is on p99 total latency, so this is the only chart it belongs on.
  { title: "Total latency", p50: (p) => p.total_p50_ms, p99: (p) => p.total_p99_ms, hasSlo: true },
];

export function Latency({ state }: { state: MonitorState }) {
  const snapshot = state.snapshot;
  if (snapshot === null) return <p className="empty">Waiting for the first snapshot…</p>;
  return (
    <>
      <p className="muted lead">
        Each point is the nearest-rank percentile over the requests that finished in that second,
        measured from when the request arrived. A second with few requests is a small sample.
      </p>
      {Object.entries(snapshot.models).map(([name, model]) => {
        const points = state.modelSeries[name] ?? [];
        const slo = model.slo.slo_p99_ms;
        return (
          <section key={name} className="model-section" aria-label={`${name} latency`}>
            <h3>{name}</h3>
            <div className="panels">
              {METRICS.map((metric) => (
                <ChartPanel
                  key={metric.title}
                  title={metric.title}
                  unit="ms"
                  columns={toColumns(points, [metric.p50, metric.p99], snapshot.resolution_ms)}
                  lines={LINES}
                  nowMs={snapshot.at_ms}
                  stepMs={snapshot.resolution_ms}
                  reference={
                    metric.hasSlo && slo !== null
                      ? { label: `p99 SLO ${formatValue(slo)} ms`, value: slo }
                      : null
                  }
                  note={metric.hasSlo && slo === null ? "No SLO is set for this model." : undefined}
                />
              ))}
            </div>
          </section>
        );
      })}
    </>
  );
}
