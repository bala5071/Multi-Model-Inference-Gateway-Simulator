// Every place in the one page, in the URL's hash (docs/50). Pure: read and written here only.
//
//   #/home                                  the home screen (also an empty hash)
//   #/monitor/<tab>                         the monitoring, one of its six tabs
//   #/organization                          the organization's settings
//   #/application[/<id>]                    an application's settings
//   #/rules[/<id>]                          an application's alert rules
//   #/channels                              the organization's alert channels
//   #/incidents[/<app>[/<incident>]]        incidents
//   #/apps                                  the organization's applications (docs/51)
//   #/apps/new                              add an application
//   #/keys, #/app/<id>/keys                 an application's API keys
//
// The dashboard's old hashes (`#/latency`, `#latency`) are read as `#/monitor/latency`, so old
// links keep working. Anything else is `unknown`: the shell answers it as it answers a place the
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

export type Route =
  | { screen: "home" }
  | { screen: "monitor"; tab: MonitorTab }
  | { screen: "application"; applicationId: string | null }
  | { screen: "organization" }
  | { screen: "rules"; applicationId: string | null }
  | { screen: "channels" }
  | { screen: "incidents"; applicationId: string | null; incidentId: string | null }
  | { screen: "apps" }
  | { screen: "apps-new" }
  | { screen: "keys"; applicationId: string | null }
  | { screen: "unknown" };

function isTab(name: string | undefined): name is MonitorTab {
  return MONITOR_TABS.some((tab) => tab === name);
}

export function parseRoute(hash: string): Route {
  const parts = hash.replace(/^#\/?/, "").split("/");
  const part = (i: number) => (parts[i] ? decodeURIComponent(parts[i]) : null);
  const [first, second] = parts;
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
  if (first === "keys" && only) return { screen: "keys", applicationId: null };
  if (first === "app" && parts.length === 3 && parts[2] === "keys" && part(1) !== null) {
    return { screen: "keys", applicationId: part(1) };
  }
  if (first === "rules" && parts.length <= 2) return { screen: "rules", applicationId: part(1) };
  if (first === "application" && parts.length <= 2) {
    return { screen: "application", applicationId: part(1) };
  }
  if (first === "incidents" && parts.length <= 3) {
    const applicationId = part(1);
    return {
      screen: "incidents",
      applicationId,
      incidentId: applicationId === null ? null : part(2),
    };
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
    case "apps":
      return "#/apps";
    case "apps-new":
      return "#/apps/new";
    case "keys":
      return route.applicationId === null ? "#/keys" : `#/app/${e(route.applicationId)}/keys`;
    case "rules":
      return route.applicationId === null ? "#/rules" : `#/rules/${e(route.applicationId)}`;
    case "incidents":
      if (route.applicationId === null) return "#/incidents";
      return route.incidentId === null
        ? `#/incidents/${e(route.applicationId)}`
        : `#/incidents/${e(route.applicationId)}/${e(route.incidentId)}`;
    case "application":
      return route.applicationId === null
        ? "#/application"
        : `#/application/${e(route.applicationId)}`;
  }
}

/** The application a route names, carried from one screen to the next where it fits. */
export function routeApplication(route: Route): string | null {
  return "applicationId" in route ? route.applicationId : null;
}
