// The page's navigation (docs/50): each item, and the gateway actions any one of which shows it.
// This is the frontend's one map from a place to an action. Whether an action is allowed is
// never decided here: it is read from GET /v1/auth/capabilities, which the gateway answers with
// the same `authorize` its routes use. Hiding an item is a convenience; every route still checks.

import type { Capabilities } from "../console/types";
import { routeApplication, type Route } from "./routes";

export type Level = "platform" | "organization" | "application";

/** An action asked of the gateway, at the level of resource it acts on. */
export interface Ask {
  level: Level;
  action: string;
}

export type ItemId =
  "home" | "monitor" | "organization" | "application" | "rules" | "channels" | "incidents";

export interface NavItem {
  id: ItemId;
  label: string;
  /** Shown when the gateway allows any one of these; with none, always shown. */
  asks: readonly Ask[];
}

export const NAVIGATION: readonly NavItem[] = [
  { id: "home", label: "Home", asks: [] },
  { id: "monitor", label: "Monitoring", asks: [{ level: "platform", action: "monitoring.read" }] },
  {
    id: "organization",
    label: "Organization settings",
    asks: [{ level: "organization", action: "org_settings.read" }],
  },
  {
    id: "application",
    label: "Application settings",
    asks: [{ level: "application", action: "settings.read" }],
  },
  {
    id: "rules",
    label: "Alert rules",
    asks: [{ level: "application", action: "alert_rules.read" }],
  },
  {
    id: "channels",
    label: "Channels",
    asks: [{ level: "organization", action: "alert_channels.read" }],
  },
  {
    id: "incidents",
    label: "Incidents",
    asks: [
      { level: "organization", action: "incidents.read_organization" },
      { level: "application", action: "incidents.read" },
    ],
  },
];

/** Inside the monitoring: the Playground tab, and the load control on it. */
export const PLAYGROUND: Ask = { level: "platform", action: "playground.use" };
export const LOAD_CONTROL: Ask = { level: "platform", action: "control.write" };
/** The organization's own incidents, for the Incidents link and the home screen's count. */
export const ORGANIZATION_INCIDENTS: Ask = {
  level: "organization",
  action: "incidents.read_organization",
};
/** For an application, its incidents: where the Incidents link goes without the above. */
export const APPLICATION_INCIDENTS: Ask = { level: "application", action: "incidents.read" };

/** What the gateway answered for `ask`: for an application action, on any it named. */
export function allows(capabilities: Capabilities, ask: Ask): boolean {
  if (ask.level === "application") {
    return capabilities.applications.some((a) => a.actions[ask.action] === true);
  }
  return capabilities[ask.level][ask.action] === true;
}

/** The applications the gateway named with `action` allowed on them, in its order. */
export function namedWith(capabilities: Capabilities, action: string): string[] {
  return capabilities.applications
    .filter((a) => a.actions[action] === true)
    .map((a) => a.application_id);
}

export function isShown(item: NavItem, capabilities: Capabilities): boolean {
  return item.asks.length === 0 || item.asks.some((ask) => allows(capabilities, ask));
}

export function shownItems(capabilities: Capabilities): NavItem[] {
  return NAVIGATION.filter((item) => isShown(item, capabilities));
}

/** The item a route belongs to; none for a route the page does not know. */
export function itemOf(route: Route): ItemId | null {
  return route.screen === "unknown" ? null : route.screen;
}

/**
 * Whether the page draws `route` for this session: its item is shown (and, for the Playground
 * tab, the Playground is allowed). An application named in the route is not checked here: the
 * screen asks the gateway, which refuses one the session cannot use (the answer lists only the
 * first page of an organization's applications, so it cannot be the judge of one id).
 */
export function opens(route: Route, capabilities: Capabilities): boolean {
  const id = itemOf(route);
  const item = NAVIGATION.find((candidate) => candidate.id === id);
  if (item === undefined || !isShown(item, capabilities)) return false;
  if (route.screen === "monitor" && route.tab === "playground") {
    return allows(capabilities, PLAYGROUND);
  }
  return true;
}

/** Where an item's link goes from `current`: the application in view is carried along. */
export function target(item: NavItem, current: Route, capabilities: Capabilities): Route {
  const application = routeApplication(current);
  switch (item.id) {
    case "home":
      return { screen: "home" };
    case "monitor":
      return current.screen === "monitor" ? current : { screen: "monitor", tab: "overview" };
    case "organization":
      return { screen: "organization" };
    case "channels":
      return { screen: "channels" };
    case "application":
      return { screen: "application", applicationId: application };
    case "rules":
      return { screen: "rules", applicationId: application };
    case "incidents": {
      // Without the organization's list, the first application whose incidents it may read.
      const first = allows(capabilities, ORGANIZATION_INCIDENTS)
        ? null
        : (namedWith(capabilities, APPLICATION_INCIDENTS.action)[0] ?? null);
      return { screen: "incidents", applicationId: application ?? first, incidentId: null };
    }
  }
}
