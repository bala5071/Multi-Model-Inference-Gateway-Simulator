import { useState } from "react";
import { formatValue } from "../charts/transforms";
import type { Control } from "../hooks/useControl";
import { Unavailable } from "./Unavailable";

/** Starts and stops the gateway's own seeded load generator, one preset at a time. */
export function LoadControl({ control }: { control: Control }) {
  const [preset, setPreset] = useState<string | null>(null);
  const [seed, setSeed] = useState<string | null>(null);
  const status = control.status;

  if (status === null) return <p className="empty">Asking the gateway…</p>;
  if (!status.load.available || status.load.presets === null) {
    return (
      <>
        <Unavailable what="Load control" />
        <p className="muted">
          It is off unless the gateway is started with <code>GATEWAY_LOAD_CONTROL=1</code> or in
          simulated live mode, because it lets anyone who can reach the gateway put load on it.
        </p>
      </>
    );
  }

  const presets = status.load.presets;
  const load = status.load.status;
  const running = load?.running === true;
  const chosen = preset ?? load?.preset ?? presets[0]?.name ?? "";
  const seedText = seed ?? String(load?.seed ?? status.seed);
  const seedNumber = Number.parseInt(seedText, 10);
  const description = presets.find((p) => p.name === chosen)?.description;

  return (
    <div className="load">
      <div className="filters">
        <label className="choice">
          Preset
          <select
            value={chosen}
            onChange={(event) => {
              setPreset(event.target.value);
            }}
          >
            {presets.map((option) => (
              <option key={option.name} value={option.name}>
                {option.name}
              </option>
            ))}
          </select>
        </label>
        <label className="choice">
          Seed
          <input
            type="number"
            value={seedText}
            onChange={(event) => {
              setSeed(event.target.value);
            }}
          />
        </label>
        <button
          type="button"
          disabled={!Number.isFinite(seedNumber)}
          onClick={() => {
            void control.start(chosen, seedNumber);
          }}
        >
          {running ? "Restart" : "Start"}
        </button>
        <button
          type="button"
          disabled={!running}
          onClick={() => {
            void control.stop();
          }}
        >
          Stop
        </button>
      </div>
      {description !== undefined && <p className="muted">{description}</p>}
      {control.error !== null && (
        <p className="form-error" role="alert">
          {control.error}
        </p>
      )}
      <p role="status" aria-label="Load status">
        {running
          ? `Running “${load.preset ?? ""}” with seed ${load.seed ?? ""}.`
          : load !== null && load.preset !== null
            ? `Stopped. The last run was “${load.preset}” with seed ${load.seed ?? ""}.`
            : "No load has been started."}
      </p>
      {load !== null && load.preset !== null && (
        <dl className="facts">
          <div>
            <dt>Sent</dt>
            <dd>{formatValue(load.sent)}</dd>
          </div>
          <div>
            <dt>Served</dt>
            <dd>{formatValue(load.served)}</dd>
          </div>
          <div>
            <dt>Refused</dt>
            <dd>{formatValue(load.refused)}</dd>
          </div>
          <div>
            <dt>In progress</dt>
            <dd>{formatValue(load.in_progress)}</dd>
          </div>
          {load.skipped > 0 && (
            <div>
              <dt>Skipped</dt>
              <dd>{formatValue(load.skipped)}</dd>
            </div>
          )}
        </dl>
      )}
    </div>
  );
}
