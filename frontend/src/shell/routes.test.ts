import {
  APP_TABS,
  crumbs,
  MONITOR_TABS,
  parseRoute,
  routeApplication,
  routeHash,
  type Route,
} from "./routes";

const page = (
  applicationId: string,
  tab: (typeof APP_TABS)[number],
  incidentId: string | null = null,
) => ({ screen: "app", applicationId, tab, incidentId }) as const;

const EVERY: Route[] = [
  { screen: "home" },
  ...MONITOR_TABS.map((tab) => ({ screen: "monitor", tab }) as const),
  { screen: "organization" },
  { screen: "channels" },
  { screen: "incidents" },
  { screen: "apps" },
  { screen: "apps-new" },
  ...APP_TABS.map((tab) => page("north-shop", tab)),
  ...APP_TABS.map((tab) => page("app one/two", tab)),
  page("north-shop", "incidents", "inc_1"),
  page("app one/two", "incidents", "inc 1/2"),
];

describe("routes", () => {
  it("round-trips every place", () => {
    for (const route of EVERY) expect(parseRoute(routeHash(route))).toEqual(route);
  });

  it("opens home on an empty hash", () => {
    for (const hash of ["", "#", "#/", "#/home"])
      expect(parseRoute(hash)).toEqual({ screen: "home" });
  });

  it("reads the dashboard's old hashes as the monitoring's tabs", () => {
    for (const tab of MONITOR_TABS) {
      expect(parseRoute(`#/${tab}`)).toEqual({ screen: "monitor", tab });
      expect(parseRoute(`#${tab}`)).toEqual({ screen: "monitor", tab });
    }
    expect(parseRoute("#/monitor")).toEqual({ screen: "monitor", tab: "overview" });
  });

  it("reads the console's old hashes as the application page's tabs (console-ia-v2-plan 2.2)", () => {
    expect(parseRoute("#/application/north-shop")).toEqual(page("north-shop", "settings"));
    expect(parseRoute("#/rules/north-shop")).toEqual(page("north-shop", "rules"));
    expect(parseRoute("#/incidents/north-shop")).toEqual(page("north-shop", "incidents"));
    expect(parseRoute("#/incidents/north-shop/inc_1")).toEqual(
      page("north-shop", "incidents", "inc_1"),
    );
    expect(parseRoute("#/app/north-shop/keys")).toEqual(page("north-shop", "keys"));
    expect(parseRoute("#/app/north-shop")).toEqual(page("north-shop", "overview"));
    // The organization's incident list stays where it was.
    expect(parseRoute("#/incidents")).toEqual({ screen: "incidents" });
    // Without an application they name none: the shell takes them to the landing.
    const none = { screen: "app", applicationId: null, incidentId: null };
    expect(parseRoute("#/application")).toEqual({ ...none, tab: "settings" });
    expect(parseRoute("#/rules")).toEqual({ ...none, tab: "rules" });
    expect(parseRoute("#/keys")).toEqual({ ...none, tab: "keys" });
    expect(parseRoute("#/app")).toEqual({ ...none, tab: "overview" });
    // Each old hash, written again, is the new address.
    expect(routeHash(parseRoute("#/application/north-shop"))).toBe("#/app/north-shop/settings");
    expect(routeHash(parseRoute("#/incidents/north-shop/inc_1"))).toBe(
      "#/app/north-shop/incidents/inc_1",
    );
  });

  it("escapes the ids it writes and reads them back", () => {
    expect(routeHash(page("a/b", "settings"))).toBe("#/app/a%2Fb/settings");
    expect(parseRoute("#/application/app%20one")).toEqual(page("app one", "settings"));
  });

  it("calls anything else unknown, and writes unknown as home", () => {
    for (const hash of [
      "#/nowhere",
      "#/monitor/nowhere",
      "#/monitor/latency/extra",
      "#/organization/extra",
      "#/channels/x",
      "#/application/a/b",
      "#/incidents/a/b/c",
      "#/home/x",
      "#/manage/users",
      "#/apps/x",
      "#/apps/new/x",
      "#/app//keys",
      "#/app/north-shop/nowhere",
      "#/app/north-shop/keys/x",
      "#/app/north-shop/incidents/a/b",
      "#/keys/north-shop",
    ]) {
      expect(parseRoute(hash)).toEqual({ screen: "unknown" });
    }
    expect(routeHash({ screen: "unknown" })).toBe("#/home");
  });

  it("writes the plan's addresses (console-ia-v2-plan 2.2)", () => {
    expect(routeHash({ screen: "apps" })).toBe("#/apps");
    expect(routeHash({ screen: "apps-new" })).toBe("#/apps/new");
    for (const tab of APP_TABS)
      expect(routeHash(page("north-shop", tab))).toBe(`#/app/north-shop/${tab}`);
    // An incident id is written only on the Incidents tab.
    expect(routeHash(page("north-shop", "settings", "inc_1"))).toBe("#/app/north-shop/settings");
  });

  it("carries a route's application", () => {
    expect(routeApplication(page("x", "rules"))).toBe("x");
    expect(routeApplication({ screen: "channels" })).toBeNull();
  });
});

describe("crumbs (docs/54, docs/56)", () => {
  const apps = { screen: "apps" } as const;
  const home = { screen: "home" } as const;
  const words = (route: Route, landing: Route = apps, name: string | null = null) =>
    crumbs(route, "north", landing, { listsApplications: true, applicationName: name }).map(
      (c) => c.label,
    );

  it("names Home after what it shows, and every other place by its section", () => {
    expect(words(home)).toEqual(["north", "Applications"]);
    expect(words(home, home)).toEqual(["Home"]);
    expect(words({ screen: "unknown" }, home)).toEqual(["Home"]);
    expect(words({ screen: "monitor", tab: "latency" })).toEqual(["Monitoring", "Latency"]);
    expect(words({ screen: "organization" })).toEqual(["north", "Organization settings"]);
    expect(words({ screen: "channels" })).toEqual(["north", "Channels"]);
    expect(words({ screen: "incidents" })).toEqual(["north", "Incidents"]);
    expect(words({ screen: "apps-new" })).toEqual(["north", "Applications", "Add application"]);
  });

  it("says Applications / name on an application's page, then the tab", () => {
    expect(words(page("north-shop", "overview"), apps, "North shop")).toEqual([
      "north",
      "Applications",
      "North shop",
    ]);
    expect(words(page("north-shop", "keys"), apps, "North shop")).toEqual([
      "north",
      "Applications",
      "North shop",
      "API keys",
    ]);
    // Until the page has read the name, the id.
    expect(words(page("north-shop", "rules"))).toEqual([
      "north",
      "Applications",
      "north-shop",
      "Alert rules",
    ]);
    expect(words(page("north-shop", "incidents", "inc_1"), apps, "North shop")).toEqual([
      "north",
      "Applications",
      "North shop",
      "Incidents",
      "inc_1",
    ]);
    // Home, for a session that lands on its one application, is that page.
    expect(words(home, page("north-shop", "overview"), "North shop")).toEqual([
      "north",
      "Applications",
      "North shop",
    ]);
  });

  it("links Applications only for a session that may open the list", () => {
    const route = page("north-shop", "settings");
    const listed = crumbs(route, "north", apps, { listsApplications: true, applicationName: null });
    const unlisted = crumbs(route, "north", route, {
      listsApplications: false,
      applicationName: null,
    });
    expect(listed[1]?.route).toEqual({ screen: "apps" });
    expect(unlisted[1]?.route).toBeNull();
  });

  it("links every step but the last, which is where the session is", () => {
    for (const route of EVERY) {
      const trail = crumbs(route, "north", apps, {
        listsApplications: true,
        applicationName: null,
      });
      expect(trail.at(-1)?.route).toBeNull();
      // Each link goes somewhere the page knows.
      for (const crumb of trail.slice(0, -1)) {
        if (crumb.route !== null) expect(parseRoute(routeHash(crumb.route))).toEqual(crumb.route);
      }
    }
  });
});
