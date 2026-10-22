import { useCallback, useEffect, useState } from "react";
import { fetchControl, setSpeed, startLoad, stopLoad, type ControlStatus } from "../api/control";

export interface Control {
  /** null until the gateway has answered once. */
  status: ControlStatus | null;
  /** The last thing that went wrong, as a sentence; null when the last call worked. */
  error: string | null;
  start: (preset: string, seed: number) => Promise<void>;
  stop: () => Promise<void>;
  speed: (speed: number) => Promise<void>;
}

/**
 * What GET /v1/control says, asked again every `pollMs`, with the three actions. An
 * action's own answer is the new status, so the screen follows the click at once.
 */
export function useControl(pollMs = 2000): Control {
  const [status, setStatus] = useState<ControlStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = useCallback(async (request: () => Promise<ControlStatus>) => {
    try {
      setStatus(await request());
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The gateway could not be reached.");
    }
  }, []);

  useEffect(() => {
    let stopped = false;
    const poll = () => {
      void fetchControl().then(
        (answer) => {
          if (!stopped) setStatus(answer);
        },
        () => undefined, // The stream's banner already says when the gateway is away.
      );
    };
    poll();
    const timer = setInterval(poll, pollMs);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [pollMs]);

  return {
    status,
    error,
    start: useCallback((preset, seed) => run(() => startLoad(preset, seed)), [run]),
    stop: useCallback(() => run(stopLoad), [run]),
    speed: useCallback((value) => run(() => setSpeed(value)), [run]),
  };
}
