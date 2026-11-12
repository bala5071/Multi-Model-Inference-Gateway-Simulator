// The page's navigation (docs/50): each item, and the gateway actions any one of which shows it.
// This is the frontend's one map from a place to an action. Whether an action is allowed is
// never decided here: it is read from GET /v1/auth/capabilities, which the gateway answers with
// the same `authorize` its routes use. Hiding an item is a convenience; every route still checks.

import type { Capabilities } from "../console/types";
import { APP_TABS, type AppTab, type Route } from "./routes";

export type Level = "platform" | "organization" | "application";

/** An action asked of the gateway, at the level of resource it acts on. */
export interface Ask {
  level: Level;
  action: string;
}

export type ItemId = "home" | "monitor" | "apps" | "organization" | "channels" | "incidents";

/**
 * The sidebar's groups (docs/54): the level of what an item opens. Home has none. "Application"
 * holds the applications a session that cannot list its organization's is named for (docs/56).
 */
export type NavGroup = "Organization" | "Application" | "Platform";

export interface NavItem {
  id: ItemId;
  label: string;
  /** Where the sidebar files it; null for Home, above the groups. */
  group: NavGroup | null;
  /** Shown when the gateway allows any one of these; with none, always shown. */
  asks: readonly Ask[];
}

/** In the sidebar's order: Home, then each group's items. */
export const NAVIGATION: readonly NavItem[] = [
  { id: "home", label: "Home", group: null, asks: [] },
  {
    id: "apps",
    label: "Applications",
    group: "Organization",
    asks: [{ level: "organization", action: "applications.list" }],
  },
  {
    id: "organization",
    label: "Organization settings",
    group: "Organization",
    asks: [{ level: "organization", action: "org_settings.read" }],
  },
  {
    id: "channels",
    label: "Channels",
    group: "Organization",
    asks: [{ level: "organization", action: "alert_channels.read" }],
  },
  {
    id: "incidents",
    label: "Incidents",
    group: "Organization",
    asks: [{ level: "organization", action: "incidents.read_organization" }],
  },
  {
    id: "monitor",
    label: "Monitoring",
    group: "Platform",
    asks: [{ level: "platform", action: "monitoring.read" }],
  },
];

/** Inside the monitoring: the Playground tab, and the load control on it. */
export const PLAYGROUND: Ask = { level: "platform", action: "playground.use" };
export const LOAD_CONTROL: Ask = { level: "platform", action: "control.write" };
/** Adding an application: the Add application control and form (docs/51). */
export const CREATE_APPLICATION: Ask = { level: "organization", action: "applications.create" };
/** The organization's application list: where such a session lands (docs/51). */
export const LIST_APPLICATIONS: Ask = { level: "organization", action: "applications.list" };
/** The organization's own incidents, for the Incidents link and the home screen's count. */
export const ORGANIZATION_INCIDENTS: Ask = {
  level: "organization",
  action: "incidents.read_organization",
};
/**
 * Each tab of an application's page and the application action that shows it (docs/56). The
 * Overview follows `applications.read`: the page itself (its figures follow
 * `usage.read_organization`, said on the tab).
 */
export const APP_TAB_ACTIONS: Record<AppTab, string> = {
  overview: "applications.read",
  settings: "settings.read",
  rules: "alert_rules.read",
  incidents: "incidents.read",
  keys: "application_keys.list",
};

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

/**
 * The tabs of an application's page the gateway allows this session, in their order: those it
 * allowed on that application when it named it; for one it did not name (the answer lists only
 * the first page of an organization's applications), those it allowed on any, and the tab's own
 * screen then asks the gateway, which refuses one the session cannot use.
 */
export function appTabs(capabilities: Capabilities, applicationId: string): AppTab[] {
  const named = capabilities.applications.find((a) => a.application_id === applicationId);
  return APP_TABS.filter((tab) =>
    named !== undefined
      ? named.actions[APP_TAB_ACTIONS[tab]] === true
      : allows(capabilities, { level: "application", action: APP_TAB_ACTIONS[tab] }),
  );
}

/**
 * The applications the sidebar lists by name (docs/56): for a session that cannot list its
 * organization's, the ones the gateway named for it with their page allowed. None otherwise:
 * such a session opens them from the list.
 */
export function appLinks(capabilities: Capabilities): string[] {
  if (allows(capabilities, LIST_APPLICATIONS)) return [];
  return namedWith(capabilities, APP_TAB_ACTIONS.overview);
}

/**
 * What a route belongs to, for asking the capabilities again at each new section: an item, or
 * an application's page (one per application). None for a route the page does not know.
 */
export function itemOf(route: Route): string | null {
  if (route.screen === "unknown") return null;
  if (route.screen === "apps-new") return "apps";
  if (route.screen === "app") return `app:${route.applicationId ?? ""}`;
  return route.screen;
}

/**
 * Where `#/home` takes this session (console-ia-v2-plan 2.1): its organization's Applications when
 * the gateway allows it the list; the page of its one application when it names exactly one;
 * Home otherwise (a platform session until Clients, one of several applications, no access).
 */
export function landing(capabilities: Capabilities): Route {
  if (allows(capabilities, LIST_APPLICATIONS)) return { screen: "apps" };
  const own = appLinks(capabilities);
  return own.length === 1 && own[0] !== undefined
    ? { screen: "app", applicationId: own[0], tab: "overview", incidentId: null }
    : { screen: "home" };
}

/**
 * Whether the page draws `route` for this session: its item is shown (and, for the Playground
 * tab, the Playground is allowed). An application named in the route is not checked here: the
 * screen asks the gateway, which refuses one the session cannot use (the answer lists only the
 * first page of an organization's applications, so it cannot be the judge of one id).
 */
export function opens(route: Route, capabilities: Capabilities): boolean {
  if (route.screen === "app") {
    // No application named: the shell takes it to the landing, for a session the gateway lets
    // use that tab on some application (an old `#/keys` and the like).
    if (route.applicationId === null) {
      return allows(capabilities, { level: "application", action: APP_TAB_ACTIONS[route.tab] });
    }
    return appTabs(capabilities, route.applicationId).includes(route.tab);
  }
  const id = itemOf(route);
  const item = NAVIGATION.find((candidate) => candidate.id === id);
  if (item === undefined || !isShown(item, capabilities)) return false;
  if (route.screen === "monitor" && route.tab === "playground") {
    return allows(capabilities, PLAYGROUND);
  }
  if (route.screen === "apps-new") return allows(capabilities, CREATE_APPLICATION);
  return true;
}

/** Where an item's link goes from `current`. */
export function target(item: NavItem, current: Route): Route {
  switch (item.id) {
    case "home":
      return { screen: "home" };
    case "monitor":
      return current.screen === "monitor" ? current : { screen: "monitor", tab: "overview" };
    case "organization":
      return { screen: "organization" };
    case "channels":
      return { screen: "channels" };
    case "apps":
      return { screen: "apps" };
    case "incidents":
      return { screen: "incidents" };
  }
}
