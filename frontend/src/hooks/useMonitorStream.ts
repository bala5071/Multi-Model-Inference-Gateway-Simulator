import { useEffect, useReducer, useState } from "react";
import { EMPTY_STATE, reduce, type MonitorState } from "../api/store";
import type { GatewayEvent, Snapshot } from "../api/types";

export type StreamStatus = "connecting" | "live" | "disconnected";

export interface StreamOptions {
  url?: string;
  /** How much history to ask for on every connection, in seconds. */
  historyS?: number;
  /** With no snapshot for this long the connection counts as lost. The gateway pushes every second. */
  staleAfterMs?: number;
  /** First wait before reconnecting; it doubles on each failure in a row. */
  retryBaseMs?: number;
  retryMaxMs?: number;
}

export interface MonitorStream {
  state: MonitorState;
  status: StreamStatus;
  /** Wall-clock time (Date.now) of the last snapshot received; null before the first. */
  lastUpdate: number | null;
}

/**
 * Keeps one server-sent-event connection to /v1/monitor/stream and folds what it pushes
 * into a MonitorState.
 *
 * Reconnecting is done here and not left to EventSource: its own retry gives up on an HTTP
 * error, and a connection that stays open but silent never errors at all. Each new
 * connection asks for the whole history again, which fills the gap, and names the last
 * event seen, so that events published while disconnected still arrive.
 */
export function useMonitorStream({
  url = "/v1/monitor/stream",
  historyS = 900,
  staleAfterMs = 5000,
  retryBaseMs = 1000,
  retryMaxMs = 15_000,
}: StreamOptions = {}): MonitorStream {
  const [state, dispatch] = useReducer(reduce, EMPTY_STATE);
  const [status, setStatus] = useState<StreamStatus>("connecting");
  const [lastUpdate, setLastUpdate] = useState<number | null>(null);

  useEffect(() => {
    let source: EventSource | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let staleTimer: ReturnType<typeof setTimeout> | undefined;
    let failures = 0;
    let lastSeq = 0;

    const fail = () => {
      source?.close();
      source = null;
      clearTimeout(staleTimer);
      setStatus("disconnected");
      const delay = Math.min(retryMaxMs, retryBaseMs * 2 ** failures);
      failures += 1;
      retryTimer = setTimeout(connect, delay);
    };

    const watch = () => {
      clearTimeout(staleTimer);
      staleTimer = setTimeout(fail, staleAfterMs);
    };

    function connect() {
      const query = new URLSearchParams({
        history_s: String(historyS),
        last_event_id: String(lastSeq),
      });
      const opened = new EventSource(`${url}?${query.toString()}`);
      source = opened;
      watch();
      opened.addEventListener("snapshot", (message: MessageEvent<string>) => {
        const snapshot = JSON.parse(message.data) as Snapshot;
        // A gateway that restarted counts its events from 1 again.
        if (snapshot.events.last_seq < lastSeq) lastSeq = snapshot.events.last_seq;
        failures = 0;
        dispatch({ kind: "snapshot", snapshot });
        setStatus("live");
        setLastUpdate(Date.now());
        watch();
      });
      opened.addEventListener("event", (message: MessageEvent<string>) => {
        const event = JSON.parse(message.data) as GatewayEvent;
        lastSeq = Math.max(lastSeq, event.seq);
        dispatch({ kind: "event", event });
      });
      opened.addEventListener("events_missed", (message: MessageEvent<string>) => {
        const { missed } = JSON.parse(message.data) as { missed: number };
        dispatch({ kind: "missed", missed });
      });
      opened.onerror = fail;
    }

    connect();
    return () => {
      source?.close();
      clearTimeout(retryTimer);
      clearTimeout(staleTimer);
    };
  }, [url, historyS, staleAfterMs, retryBaseMs, retryMaxMs]);

  return { state, status, lastUpdate };
}
