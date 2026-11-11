// The navigation's icons (docs/54): Lucide's, imported one by one so only these are bundled.
// Decorative only: every item keeps its label as its name.
import {
  Activity,
  BellRing,
  Building2,
  House,
  KeyRound,
  LayoutGrid,
  Send,
  SlidersHorizontal,
  TriangleAlert,
  type LucideIcon,
} from "lucide-react";
import type { ItemId } from "./navigation";

export const ITEM_ICONS: Record<ItemId, LucideIcon> = {
  home: House,
  apps: LayoutGrid,
  organization: Building2,
  channels: Send,
  incidents: TriangleAlert,
  application: SlidersHorizontal,
  rules: BellRing,
  keys: KeyRound,
  monitor: Activity,
};
