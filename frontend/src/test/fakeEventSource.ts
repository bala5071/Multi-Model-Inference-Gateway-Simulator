import { act } from "@testing-library/react";

type Listener = (message: MessageEvent<string>) => void;

/** An EventSource the test drives by hand. Every one created is kept in `instances`. */
export class FakeEventSource {
  static instances: FakeEventSource[] = [];

  onerror: (() => void) | null = null;
  closed = false;
  private listeners = new Map<string, Listener[]>();

  constructor(readonly url: string) {
    FakeEventSource.instances.push(this);
  }

  static get latest(): FakeEventSource {
    const latest = FakeEventSource.instances.at(-1);
    if (latest === undefined) throw new Error("no EventSource has been opened");
    return latest;
  }

  addEventListener(name: string, listener: Listener): void {
    this.listeners.set(name, [...(this.listeners.get(name) ?? []), listener]);
  }

  close(): void {
    this.closed = true;
  }

  /** Deliver one server-sent event. */
  push(name: string, data: unknown): void {
    act(() => {
      const message = new MessageEvent<string>(name, { data: JSON.stringify(data) });
      for (const listener of this.listeners.get(name) ?? []) listener(message);
    });
  }

  /** Break the connection, as the browser reports it. */
  fail(): void {
    act(() => {
      this.onerror?.();
    });
  }

  get query(): URLSearchParams {
    return new URL(this.url, "http://gateway").searchParams;
  }
}

export function installFakeEventSource(): void {
  FakeEventSource.instances = [];
  vi.stubGlobal("EventSource", FakeEventSource);
}
