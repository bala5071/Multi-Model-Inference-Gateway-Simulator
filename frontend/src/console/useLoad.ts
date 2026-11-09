import { useEffect, useState } from "react";
import { ApiError } from "./api";

export type Loaded<T> =
  { kind: "loading" } | { kind: "failed"; error: ApiError } | { kind: "ready"; value: T };

function asApiError(error: unknown): ApiError {
  return error instanceof ApiError
    ? error
    : new ApiError(0, "unexpected", "Something went wrong reading the gateway's answer.");
}

/**
 * Runs `load` when `key` changes and keeps its latest answer; an answer to an older key is
 * dropped. `key` names everything `load` reads, so a reload is a new key.
 */
export function useLoad<T>(key: string, load: () => Promise<T>): Loaded<T> {
  const [state, setState] = useState<{ key: string; loaded: Loaded<T> }>({
    key,
    loaded: { kind: "loading" },
  });

  useEffect(() => {
    let current = true;
    load().then(
      (value) => {
        if (current) setState({ key, loaded: { kind: "ready", value } });
      },
      (error: unknown) => {
        if (current) setState({ key, loaded: { kind: "failed", error: asApiError(error) } });
      },
    );
    return () => {
      current = false;
    };
    // `key` stands for everything `load` reads.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  return state.key === key ? state.loaded : { kind: "loading" };
}
