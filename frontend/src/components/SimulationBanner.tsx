import type { Control } from "../hooks/useControl";

/**
 * Shown on every tab while the gateway is in simulated live mode, so that nothing on the
 * page is taken for a measurement of a real model or for wall-clock time.
 */
export function SimulationBanner({ control }: { control: Control }) {
  const status = control.status;
  if (status === null || !status.simulation.active) return null;
  const { speed, speeds } = status.simulation;
  const load = status.load.status;
  return (
    <div className="sim-banner" role="status" aria-label="Simulated live mode">
      <div>
        <strong>Simulated live mode.</strong> No model is running. Everything shown is a
        deterministic replay on simulated time, on model profiles calibrated once on one laptop: the
        same seed gives the same sequence. Times and rates are simulated, not wall-clock; costs use
        placeholder prices.
        <span className="sim-facts">
          {" "}
          Seed {load?.seed ?? status.seed}
          {load?.running === true && load.preset !== null && ` · load preset “${load.preset}”`}
          {load?.running !== true && " · no load running"}
        </span>
      </div>
      <div className="speeds" role="group" aria-label="Replay speed">
        <span className="muted">Speed</span>
        {(speeds ?? []).map((option) => (
          <button
            key={option}
            type="button"
            aria-pressed={option === speed}
            onClick={() => {
              void control.speed(option);
            }}
          >
            {option}×
          </button>
        ))}
      </div>
    </div>
  );
}
