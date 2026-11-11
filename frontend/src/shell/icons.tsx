// The navigation's icons (docs/54, docs/56): Lucide's, imported one by one so only these are
// bundled. Decorative only: every item and tab keeps its label as its name.
import {
  Activity,
  AppWindow,
  BellRing,
  Building2,
  Gauge,
  House,
  KeyRound,
  LayoutGrid,
  Send,
  SlidersHorizontal,
  TriangleAlert,
  type LucideIcon,
} from "lucide-react";
import type { ItemId } from "./navigation";
import type { AppTab } from "./routes";

export const ITEM_ICONS: Record<ItemId, LucideIcon> = {
  home: House,
  apps: LayoutGrid,
  organization: Building2,
  channels: Send,
  incidents: TriangleAlert,
  monitor: Activity,
};

/** An application, where the sidebar lists one by name. */
export const APP_ICON: LucideIcon = AppWindow;

/** The application page's tabs. */
export const TAB_ICONS: Record<AppTab, LucideIcon> = {
  overview: Gauge,
  settings: SlidersHorizontal,
  rules: BellRing,
  incidents: TriangleAlert,
  keys: KeyRound,
};
