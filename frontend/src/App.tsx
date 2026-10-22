import { DisconnectedBanner } from "./components/DisconnectedBanner";
import { SimulationBanner } from "./components/SimulationBanner";
import { Tabs } from "./components/Tabs";
import { useControl } from "./hooks/useControl";
import { useHashTab } from "./hooks/useHashTab";
import { useMonitorStream, type StreamOptions } from "./hooks/useMonitorStream";
import { useTheme } from "./hooks/useTheme";
import { Capacity } from "./tabs/Capacity";
import { Events } from "./tabs/Events";
import { Latency } from "./tabs/Latency";
import { Overview } from "./tabs/Overview";
import { Playground } from "./tabs/Playground";
import { Applications } from "./tabs/Applications";

const TABS = [
  { id: "overview", label: "Overview" },
  { id: "latency", label: "Latency" },
  { id: "capacity", label: "Capacity" },
  { id: "applications", label: "Applications" },
  { id: "events", label: "Events" },
  { id: "playground", label: "Playground" },
] as const;

type TabId = (typeof TABS)[number]["id"];
const TAB_IDS = TABS.map((tab) => tab.id);

const STATUS_TEXT = { connecting: "Connecting…", live: "Live", disconnected: "Disconnected" };

export function App({ stream: options }: { stream?: StreamOptions }) {
  const { state, status, lastUpdate } = useMonitorStream(options);
  const [tab, setTab] = useHashTab<TabId>(TAB_IDS, "overview");
  const { theme, toggle } = useTheme();
  const control = useControl();
  const simulated = control.status?.simulation.active === true;

  return (
    <div className={status === "disconnected" ? "app stale" : "app"}>
      <header className="top">
        <h1>Gateway monitor</h1>
        {simulated && <span className="mode-badge">Simulated</span>}
        <span className={`status status-${status}`}>{STATUS_TEXT[status]}</span>
        <button type="button" onClick={toggle}>
          {theme === "dark" ? "Light theme" : "Dark theme"}
        </button>
      </header>
      <SimulationBanner control={control} />
      <DisconnectedBanner status={status} lastUpdate={lastUpdate} />
      <Tabs tabs={TABS} selected={tab} onSelect={setTab} />
      <main id="tab-panel" role="tabpanel" aria-labelledby={`tab-${tab}`}>
        {tab === "overview" && <Overview state={state} />}
        {tab === "latency" && <Latency state={state} />}
        {tab === "capacity" && <Capacity state={state} />}
        {tab === "applications" && <Applications state={state} />}
        {tab === "events" && <Events state={state} />}
        {tab === "playground" && <Playground state={state} control={control} />}
      </main>
    </div>
  );
}
