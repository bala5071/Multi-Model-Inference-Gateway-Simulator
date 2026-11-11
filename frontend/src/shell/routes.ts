// Every place in the one page, in the URL's hash (docs/50, docs/56). Pure: read and written here
// only.
//
//   #/home                                  the home screen (also an empty hash)
//   #/monitor/<tab>                         the monitoring, one of its six tabs
//   #/organization                          the organization's settings
//   #/channels                              the organization's alert channels
//   #/incidents                             the organization's incidents
//   #/apps                                  the organization's applications (docs/51)
//   #/apps/new                              add an application
//   #/app/<id>/<tab>                        an application's page: overview, settings, rules,
//                                           incidents, keys (console-ia-v2-plan 2.2)
//   #/app/<id>/incidents/<incident>         one of its incidents
//
// Old hashes keep working (2.2): `#/application/<id>` is the page's settings, `#/rules/<id>` its
// rules, `#/incidents/<app>[/<incident>]` its incidents, `#/app/<id>` its overview;
// `#/application`, `#/rules` and `#/keys` without an application name none, and the shell takes
// them to the session's landing. The dashboard's old hashes (`#/latency`, `#latency`) are read as
// `#/monitor/latency`. Anything else is `unknown`: the shell answers it as it answers a place the
// session may not open.

export const MONITOR_TABS = [
  "overview",
  "latency",
  "capacity",
  "applications",
  "events",
  "playground",
] as const;

export type MonitorTab = (typeof MONITOR_TABS)[number];

/** The application page's tabs, in their order. */
export const APP_TABS = ["overview", "settings", "rules", "incidents", "keys"] as const;

export type AppTab = (typeof APP_TABS)[number];

export type Route =
  | { screen: "home" }
  | { screen: "monitor"; tab: MonitorTab }
  | { screen: "organization" }
  | { screen: "channels" }
  | { screen: "incidents" }
  | { screen: "apps" }
  | { screen: "apps-new" }
  | { screen: "app"; applicationId: string | null; tab: AppTab; incidentId: string | null }
  | { screen: "unknown" };

function isTab(name: string | undefined): name is MonitorTab {
  return MONITOR_TABS.some((tab) => tab === name);
}

function isAppTab(name: string | undefined): name is AppTab {
  return APP_TABS.some((tab) => tab === name);
}

function app(applicationId: string | null, tab: AppTab, incidentId: string | null = null): Route {
  return { screen: "app", applicationId, tab, incidentId };
}

export function parseRoute(hash: string): Route {
  const parts = hash.replace(/^#\/?/, "").split("/");
  const part = (i: number) => (parts[i] ? decodeURIComponent(parts[i]) : null);
  const [first, second, third] = parts;
  const only = parts.length === 1;
  if (first === "" && only) return { screen: "home" };
  if (first === "home" && only) return { screen: "home" };
  if (first === "monitor") {
    if (parts.length === 1) return { screen: "monitor", tab: "overview" };
    return isTab(second) && parts.length === 2
      ? { screen: "monitor", tab: second }
      : { screen: "unknown" };
  }
  if (only && isTab(first)) return { screen: "monitor", tab: first };
  if (first === "organization" && only) return { screen: "organization" };
  if (first === "channels" && only) return { screen: "channels" };
  if (first === "apps" && only) return { screen: "apps" };
  if (first === "apps" && second === "new" && parts.length === 2) return { screen: "apps-new" };
  if (first === "app") {
    const id = part(1);
    if (id === null) return only ? app(null, "overview") : { screen: "unknown" };
    if (parts.length === 2) return app(id, "overview");
    if (parts.length === 3 && isAppTab(third)) return app(id, third);
    if (parts.length === 4 && third === "incidents" && part(3) !== null) {
      return app(id, "incidents", part(3));
    }
    return { screen: "unknown" };
  }
  // The old hashes (2.2).
  if (first === "keys" && only) return app(null, "keys");
  if (first === "rules" && parts.length <= 2) return app(part(1), "rules");
  if (first === "application" && parts.length <= 2) return app(part(1), "settings");
  if (first === "incidents") {
    if (only) return { screen: "incidents" };
    if (parts.length <= 3 && part(1) !== null) return app(part(1), "incidents", part(2));
  }
  return { screen: "unknown" };
}

export function routeHash(route: Route): string {
  const e = encodeURIComponent;
  switch (route.screen) {
    case "home":
    case "unknown":
      return "#/home";
    case "monitor":
      return `#/monitor/${route.tab}`;
    case "organization":
      return "#/organization";
    case "channels":
      return "#/channels";
    case "incidents":
      return "#/incidents";
    case "apps":
      return "#/apps";
    case "apps-new":
      return "#/apps/new";
    case "app": {
      if (route.applicationId === null) return "#/app";
      const page = `#/app/${e(route.applicationId)}/${route.tab}`;
      return route.tab === "incidents" && route.incidentId !== null
        ? `${page}/${e(route.incidentId)}`
        : page;
    }
  }
}

/** The application a route names, carried from one screen to the next where it fits. */
export function routeApplication(route: Route): string | null {
  return route.screen === "app" ? route.applicationId : null;
}

/** One step of the breadcrumb trail: its words, and where it goes (none for the last step). */
export interface Crumb {
  label: string;
  route: Route | null;
}

const MONITOR_WORDS: Record<MonitorTab, string> = {
  overview: "Overview",
  latency: "Latency",
  capacity: "Capacity",
  applications: "Applications",
  events: "Events",
  playground: "Playground",
};

export const APP_TAB_WORDS: Record<AppTab, string> = {
  overview: "Overview",
  settings: "Settings",
  rules: "Alert rules",
  incidents: "Incidents",
  keys: "API keys",
};

/** What the trail knows beyond the route: whether the list is open to it, the page's name. */
export interface TrailContext {
  /** The session may open the organization's Applications (so the step links there). */
  listsApplications: boolean;
  /** The application's name, once the page has read it; its id until then. */
  applicationName: string | null;
}

/**
 * The breadcrumb trail of a place (docs/54): the organization, the section, then what the route
 * names in it; on an application's page "Applications / name", then the tab. Pure; a trail never
 * says whether the session may open a step, the page does. `landing` is where `#/home` takes this
 * session, so Home is named after what it shows.
 */
export function crumbs(
  route: Route,
  organizationId: string,
  landing: Route,
  context: TrailContext = { listsApplications: false, applicationName: null },
): Crumb[] {
  const org: Crumb = { label: organizationId, route: null };
  const last = (label: string): Crumb => ({ label, route: null });
  switch (route.screen) {
    case "home":
    case "unknown":
      if (landing.screen === "app") return crumbs(landing, organizationId, landing, context);
      return landing.screen === "apps" ? [org, last("Applications")] : [last("Home")];
    case "monitor":
      return [
        { label: "Monitoring", route: { screen: "monitor", tab: "overview" } },
        last(MONITOR_WORDS[route.tab]),
      ];
    case "apps":
      return [org, last("Applications")];
    case "apps-new":
      return [org, { label: "Applications", route: { screen: "apps" } }, last("Add application")];
    case "organization":
      return [org, last("Organization settings")];
    case "channels":
      return [org, last("Channels")];
    case "incidents":
      return [org, last("Incidents")];
    case "app": {
      const list: Crumb = {
        label: "Applications",
        route: context.listsApplications ? { screen: "apps" } : null,
      };
      if (route.applicationId === null) return [org, last("Applications")];
      const name = context.applicationName ?? route.applicationId;
      if (route.tab === "overview") return [org, list, last(name)];
      const page: Crumb = { label: name, route: app(route.applicationId, "overview") };
      if (route.tab === "incidents" && route.incidentId !== null) {
        return [
          org,
          list,
          page,
          { label: "Incidents", route: app(route.applicationId, "incidents") },
          last(route.incidentId),
        ];
      }
      return [org, list, page, last(APP_TAB_WORDS[route.tab])];
    }
  }
}
