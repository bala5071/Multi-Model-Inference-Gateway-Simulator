import { useEffect, useId, useRef } from "react";
import { DisconnectedBanner } from "./components/DisconnectedBanner";
import { SimulationBanner } from "./components/SimulationBanner";
import { useControl } from "./hooks/useControl";
import { useMonitorStream, type StreamOptions } from "./hooks/useMonitorStream";
import type { MonitorTab } from "./shell/routes";
import { Applications } from "./tabs/Applications";
import { Capacity } from "./tabs/Capacity";
import { Events } from "./tabs/Events";
import { Latency } from "./tabs/Latency";
import { Overview } from "./tabs/Overview";
import { Playground } from "./tabs/Playground";
import { StatusPill, type Tone } from "./ui/StatusPill";
import { TabPanel, Tabs } from "./ui/Tabs";

const TABS: readonly { id: MonitorTab; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "latency", label: "Latency" },
  { id: "capacity", label: "Capacity" },
  { id: "applications", label: "Applications" },
  { id: "events", label: "Events" },
  { id: "playground", label: "Playground" },
];

const STATUS_TEXT = { connecting: "Connecting…", live: "Live", disconnected: "Disconnected" };
/** The stream's state as a pill: its colour, beside its word (docs/56). */
const STATUS_TONE: Record<keyof typeof STATUS_TEXT, Tone> = {
  connecting: "warn",
  live: "ok",
  disconnected: "bad",
};

/** What the gateway allows inside the monitoring; everything without accounts. */
export interface MonitoringAllows {
  playground: boolean;
  loadControl: boolean;
}

const EVERYTHING: MonitoringAllows = { playground: true, loadControl: true };

export interface MonitoringProps {
  /** The tab in view: the shell keeps it in the URL (`#/monitor/<tab>`). */
  tab: MonitorTab;
  onSelectTab: (tab: MonitorTab) => void;
  stream?: StreamOptions;
  allows?: MonitoringAllows;
  /** Told whether the gateway is in simulated live mode, for the page's header. */
  onSimulated?: (active: boolean) => void;
  /** Told when the stream comes back after a break, so the shell can ask after the session. */
  onReconnect?: () => void;
}

/**
 * The monitoring (the former dashboard, docs/15 and docs/16), one section of the page. The
 * stream runs only while it is shown. It reads only the monitoring API.
 */
export function App({
  tab,
  onSelectTab,
  stream: options,
  allows = EVERYTHING,
  onSimulated,
  onReconnect,
}: MonitoringProps) {
  const { state, status, lastUpdate } = useMonitorStream(options);
  const control = useControl();
  const simulated = control.status?.simulation.active === true;
  const tabs = TABS.filter((t) => t.id !== "playground" || allows.playground);
  const shown = tabs.some((t) => t.id === tab) ? tab : "overview";
  const prefix = useId();

  useEffect(() => {
    onSimulated?.(simulated);
  }, [simulated, onSimulated]);

  const previous = useRef(status);
  useEffect(() => {
    if (previous.current === "disconnected" && status === "live") onReconnect?.();
    previous.current = status;
  }, [status, onReconnect]);

  return (
    <div className={status === "disconnected" ? "monitor stale" : "monitor"}>
      <div className="monitor-bar">
        <h2>Monitoring</h2>
        <span role="status">
          <StatusPill tone={STATUS_TONE[status]}>{STATUS_TEXT[status]}</StatusPill>
        </span>
      </div>
      <SimulationBanner control={control} />
      <DisconnectedBanner status={status} lastUpdate={lastUpdate} />
      <Tabs
        label="Monitoring sections"
        prefix={prefix}
        tabs={tabs}
        selected={shown}
        onSelect={onSelectTab}
      />
      <TabPanel prefix={prefix} selected={shown}>
        {shown === "overview" && <Overview state={state} />}
        {shown === "latency" && <Latency state={state} />}
        {shown === "capacity" && <Capacity state={state} />}
        {shown === "applications" && <Applications state={state} />}
        {shown === "events" && <Events state={state} />}
        {shown === "playground" && (
          <Playground state={state} control={control} loadControl={allows.loadControl} />
        )}
      </TabPanel>
    </div>
  );
}
