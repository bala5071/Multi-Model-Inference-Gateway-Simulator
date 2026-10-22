import type { MonitorState } from "../api/store";
import { ModelCard } from "../components/ModelCard";

export function Overview({ state }: { state: MonitorState }) {
  const snapshot = state.snapshot;
  if (snapshot === null) return <p className="empty">Waiting for the first snapshot…</p>;
  return (
    <div className="cards">
      {Object.entries(snapshot.models).map(([name, model]) => (
        <ModelCard key={name} name={name} model={model} points={state.modelSeries[name] ?? []} />
      ))}
    </div>
  );
}
