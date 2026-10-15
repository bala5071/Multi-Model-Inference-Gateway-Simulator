import { act, renderHook } from "@testing-library/react";
import type { Snapshot } from "../api/types";
import { FakeEventSource, installFakeEventSource } from "../test/fakeEventSource";
import { simulated } from "../test/fixtures";
import { useMonitorStream } from "./useMonitorStream";

const OPTIONS = { staleAfterMs: 5000, retryBaseMs: 1000, retryMaxMs: 4000 };

function advance(ms: number): void {
  act(() => {
    vi.advanceTimersByTime(ms);
  });
}

function next(seconds: number): Snapshot {
  return { ...simulated, at_ms: simulated.at_ms + seconds * 1000 };
}

beforeEach(() => {
  vi.useFakeTimers();
  installFakeEventSource();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("useMonitorStream", () => {
  it("opens the stream asking for the whole history and every retained event", () => {
    const { result } = renderHook(() => useMonitorStream(OPTIONS));

    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.latest.url.startsWith("/v1/monitor/stream?")).toBe(true);
    expect(FakeEventSource.latest.query.get("history_s")).toBe("900");
    expect(FakeEventSource.latest.query.get("last_event_id")).toBe("0");
    expect(result.current.status).toBe("connecting");
    expect(result.current.state.snapshot).toBeNull();
    expect(result.current.lastUpdate).toBeNull();
  });

  it("is live once a snapshot arrives, and folds in what follows", () => {
    const { result } = renderHook(() => useMonitorStream(OPTIONS));

    FakeEventSource.latest.push("snapshot", simulated);
    expect(result.current.status).toBe("live");
    expect(result.current.state.snapshot?.at_ms).toBe(6782);
    expect(result.current.lastUpdate).not.toBeNull();

    FakeEventSource.latest.push("event", {
      seq: 5,
      at_ms: 7000,
      type: "timeout",
      model: "premium",
      tenant_id: null,
      data: { timeout_ms: 100 },
    });
    FakeEventSource.latest.push("events_missed", { missed: 2 });
    FakeEventSource.latest.push("snapshot", next(1));

    expect(result.current.state.events.map((e) => e.seq)).toEqual([1, 2, 3, 4, 5]);
    expect(result.current.state.missedEvents).toBe(2);
    expect(result.current.state.snapshot?.at_ms).toBe(7782);
  });

  it("reports a broken connection and reconnects after the first wait", () => {
    const { result } = renderHook(() => useMonitorStream(OPTIONS));
    const first = FakeEventSource.latest;
    first.push("snapshot", simulated);

    first.fail();
    expect(result.current.status).toBe("disconnected");
    expect(first.closed).toBe(true);
    // What was received stays on screen.
    expect(result.current.state.snapshot?.at_ms).toBe(6782);

    advance(999);
    expect(FakeEventSource.instances).toHaveLength(1);
    advance(1);
    expect(FakeEventSource.instances).toHaveLength(2);
    // Still disconnected until the new connection delivers something.
    expect(result.current.status).toBe("disconnected");

    FakeEventSource.latest.push("snapshot", next(3));
    expect(result.current.status).toBe("live");
  });

  it("names the last event it saw when it reconnects", () => {
    renderHook(() => useMonitorStream(OPTIONS));
    FakeEventSource.latest.push("snapshot", simulated);
    for (const seq of [5, 6, 7]) {
      FakeEventSource.latest.push("event", { ...simulated.events.recent[0], seq });
    }

    FakeEventSource.latest.fail();
    advance(1000);

    expect(FakeEventSource.latest.query.get("last_event_id")).toBe("7");
    expect(FakeEventSource.latest.query.get("history_s")).toBe("900");
  });

  it("starts its event cursor again when the gateway has restarted", () => {
    renderHook(() => useMonitorStream(OPTIONS));
    FakeEventSource.latest.push("event", { ...simulated.events.recent[0], seq: 40 });
    // The new process has published two events in all.
    FakeEventSource.latest.push("snapshot", {
      ...simulated,
      events: { ...simulated.events, last_seq: 2, recent: [] },
    });

    FakeEventSource.latest.fail();
    advance(1000);

    expect(FakeEventSource.latest.query.get("last_event_id")).toBe("2");
  });

  it("waits twice as long after each failure in a row, up to the limit", () => {
    renderHook(() => useMonitorStream(OPTIONS));
    const waits: number[] = [];

    for (let attempt = 0; attempt < 5; attempt++) {
      const before = FakeEventSource.instances.length;
      FakeEventSource.latest.fail();
      let waited = 0;
      while (FakeEventSource.instances.length === before) {
        advance(500);
        waited += 500;
      }
      waits.push(waited);
    }

    expect(waits).toEqual([1000, 2000, 4000, 4000, 4000]);
  });

  it("goes back to the first wait once a connection has worked", () => {
    renderHook(() => useMonitorStream(OPTIONS));
    FakeEventSource.latest.fail();
    advance(1000);
    FakeEventSource.latest.fail();
    advance(2000);
    expect(FakeEventSource.instances).toHaveLength(3);

    FakeEventSource.latest.push("snapshot", simulated);
    FakeEventSource.latest.fail();
    advance(1000);

    expect(FakeEventSource.instances).toHaveLength(4);
  });

  it("treats a connection that goes silent as lost", () => {
    const { result } = renderHook(() => useMonitorStream(OPTIONS));
    const first = FakeEventSource.latest;
    first.push("snapshot", simulated);

    advance(4999);
    expect(result.current.status).toBe("live");
    advance(1);
    expect(result.current.status).toBe("disconnected");
    expect(first.closed).toBe(true);

    advance(1000);
    expect(FakeEventSource.instances).toHaveLength(2);
  });

  it("is not stale while snapshots keep coming", () => {
    const { result } = renderHook(() => useMonitorStream(OPTIONS));
    for (let second = 0; second < 10; second++) {
      FakeEventSource.latest.push("snapshot", next(second));
      advance(1000);
    }
    expect(result.current.status).toBe("live");
    expect(FakeEventSource.instances).toHaveLength(1);
  });

  it("gives up on a connection that never delivers anything", () => {
    const { result } = renderHook(() => useMonitorStream(OPTIONS));
    advance(5000);
    expect(result.current.status).toBe("disconnected");
    expect(FakeEventSource.latest.closed).toBe(true);
  });

  it("closes the connection and stops retrying when unmounted", () => {
    const { unmount } = renderHook(() => useMonitorStream(OPTIONS));
    FakeEventSource.latest.fail();

    unmount();
    advance(60_000);

    expect(FakeEventSource.instances).toHaveLength(1);

    const again = renderHook(() => useMonitorStream(OPTIONS));
    const open = FakeEventSource.latest;
    again.unmount();
    expect(open.closed).toBe(true);
    advance(60_000);
    expect(FakeEventSource.instances).toHaveLength(2);
  });
});
