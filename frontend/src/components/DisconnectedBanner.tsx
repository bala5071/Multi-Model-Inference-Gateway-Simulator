import type { StreamStatus } from "../hooks/useMonitorStream";

export interface BannerProps {
  status: StreamStatus;
  /** Date.now() of the last snapshot; null if none has arrived. */
  lastUpdate: number | null;
}

/** Shown while the stream is down. What is on screen under it is then old. */
export function DisconnectedBanner({ status, lastUpdate }: BannerProps) {
  if (status !== "disconnected") return null;
  return (
    <div className="banner" role="alert">
      <strong>Disconnected from the gateway.</strong>{" "}
      {lastUpdate === null
        ? "Nothing has been received yet."
        : `Showing data from ${new Date(lastUpdate).toLocaleTimeString()}.`}{" "}
      Retrying…
    </div>
  );
}
