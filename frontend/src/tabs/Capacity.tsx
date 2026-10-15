import type { MonitorState } from "../api/store";
import { ChartPanel } from "../charts/ChartPanel";
import { formatValue, toColumns, toPercent } from "../charts/transforms";
import { Unavailable } from "../components/Unavailable";

export function Capacity({ state }: { state: MonitorState }) {
  const snapshot = state.snapshot;
  if (snapshot === null) return <p className="empty">Waiting for the first snapshot…</p>;
  const stepMs = snapshot.resolution_ms;
  return (
    <>
      <p className="muted lead">
        Queue, batch and KV are the largest value sampled in each second. A second in which nobody
        looked at the model has no point.
      </p>
      {Object.entries(snapshot.models).map(([name, model]) => {
        const points = state.modelSeries[name] ?? [];
        const maxBatch = model.batch.max_size;
        const kvCapacity = model.kv.capacity_tokens;
        return (
          <section key={name} className="model-section" aria-label={`${name} capacity`}>
            <h3>{name}</h3>
            <div className="panels">
              {model.queue.available ? (
                <ChartPanel
                  title="Queue depth"
                  unit="requests"
                  note="Requests waiting for a batch slot."
                  columns={toColumns(points, [(p) => p.queue_len_max], stepMs)}
                  lines={[{ label: "waiting", slot: 1, reducer: "max" }]}
                  nowMs={snapshot.at_ms}
                  stepMs={stepMs}
                />
              ) : (
                <Unavailable what="Queue depth" />
              )}
              {model.batch.available && maxBatch !== null ? (
                <ChartPanel
                  title="Batch size"
                  unit="requests"
                  columns={toColumns(points, [(p) => p.batch_size_max], stepMs)}
                  lines={[{ label: "seated", slot: 1, reducer: "max" }]}
                  nowMs={snapshot.at_ms}
                  stepMs={stepMs}
                  fixedMax={maxBatch}
                  reference={{ label: `max ${formatValue(maxBatch)}`, value: maxBatch }}
                />
              ) : (
                <Unavailable what="Batch size" />
              )}
              {model.kv.available && kvCapacity !== null ? (
                <ChartPanel
                  title="KV tokens used"
                  unit="tokens"
                  columns={toColumns(points, [(p) => p.kv_used_tokens_max], stepMs)}
                  lines={[{ label: "used", slot: 1, reducer: "max" }]}
                  nowMs={snapshot.at_ms}
                  stepMs={stepMs}
                  fixedMax={kvCapacity}
                  reference={{ label: `capacity ${formatValue(kvCapacity)}`, value: kvCapacity }}
                />
              ) : (
                <Unavailable what="KV tokens used" />
              )}
              {model.cache.available ? (
                <ChartPanel
                  title="Prefix cache hit rate"
                  unit="%"
                  note="Seconds with no lookups have no rate."
                  columns={toColumns(
                    points,
                    [(p) => toPercent(p.cache_hit_rate), (p) => toPercent(p.cache_token_hit_rate)],
                    stepMs,
                  )}
                  lines={[
                    { label: "lookups that hit", slot: 1, reducer: "mean" },
                    { label: "prompt tokens cached", slot: 2, reducer: "mean" },
                  ]}
                  nowMs={snapshot.at_ms}
                  stepMs={stepMs}
                  fixedMax={100}
                />
              ) : (
                <Unavailable what="Prefix cache hit rate" />
              )}
            </div>
          </section>
        );
      })}
    </>
  );
}
