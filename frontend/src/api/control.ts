// GET /v1/control and the actions beside it: load control and simulated live mode.
// Mirrored by hand from docs/17-playground-load-replay.md; `src/test/fixtures/control-*.json`
// are what the gateway itself answered.

export interface LoadStatus {
  running: boolean;
  preset: string | null;
  seed: number | null;
  /** Gateway clock time the run started at. */
  started_ms: number | null;
  phases_started: number;
  sent: number;
  served: number;
  refused: number;
  skipped: number;
  in_progress: number;
}

export interface LoadPreset {
  name: string;
  description: string;
}

export interface ControlStatus {
  /** The gateway's own seed: what a load run uses when none is given. */
  seed: number;
  load: {
    /** False unless the gateway was started with load control. The rest is then null. */
    available: boolean;
    presets: LoadPreset[] | null;
    status: LoadStatus | null;
  };
  playground: {
    /**
     * True in simulated live mode: the gateway holds a throwaway key per application and
     * serves POST /v1/playground/generate with it. Elsewhere requests need an API key.
     */
    available: boolean;
  };
  simulation: {
    /** True in simulated live mode: no model runs and every time shown is simulated. */
    active: boolean;
    speed: number | null;
    speeds: number[] | null;
  };
}

export class ControlError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

async function call(path: string, body?: unknown): Promise<ControlStatus> {
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const refused = (await response.json().catch(() => ({}))) as {
      error?: { code?: string; message?: string };
    };
    throw new ControlError(
      response.status,
      refused.error?.code ?? "http_error",
      refused.error?.message ?? `The gateway answered HTTP ${response.status}.`,
    );
  }
  return (await response.json()) as ControlStatus;
}

export const fetchControl = () => call("/v1/control");
export const startLoad = (preset: string, seed: number) =>
  call("/v1/control/load/start", { preset, seed });
export const stopLoad = () => call("/v1/control/load/stop", {});
export const setSpeed = (speed: number) => call("/v1/control/speed", { speed });
