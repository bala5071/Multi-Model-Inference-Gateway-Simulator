import {
  crumbs,
  MONITOR_TABS,
  parseRoute,
  routeApplication,
  routeHash,
  type Route,
} from "./routes";

const EVERY: Route[] = [
  { screen: "home" },
  ...MONITOR_TABS.map((tab) => ({ screen: "monitor", tab }) as const),
  { screen: "organization" },
  { screen: "channels" },
  { screen: "application", applicationId: null },
  { screen: "application", applicationId: "app one/two" },
  { screen: "rules", applicationId: null },
  { screen: "rules", applicationId: "north-shop" },
  { screen: "incidents", applicationId: null, incidentId: null },
  { screen: "incidents", applicationId: "north-shop", incidentId: null },
  { screen: "incidents", applicationId: "north-shop", incidentId: "inc_1" },
  { screen: "apps" },
  { screen: "apps-new" },
  { screen: "keys", applicationId: null },
  { screen: "keys", applicationId: "north-shop" },
  { screen: "keys", applicationId: "app one/two" },
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

  it("escapes the ids it writes and reads them back", () => {
    expect(routeHash({ screen: "application", applicationId: "a/b" })).toBe("#/application/a%2Fb");
    expect(parseRoute("#/application/app%20one")).toEqual({
      screen: "application",
      applicationId: "app one",
    });
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
      "#/app",
      "#/app/north-shop",
      "#/app//keys",
      "#/app/north-shop/settings",
      "#/keys/north-shop",
    ]) {
      expect(parseRoute(hash)).toEqual({ screen: "unknown" });
    }
    expect(routeHash({ screen: "unknown" })).toBe("#/home");
  });

  it("writes the plan's addresses for the applications and their keys (docs/51)", () => {
    expect(routeHash({ screen: "apps" })).toBe("#/apps");
    expect(routeHash({ screen: "apps-new" })).toBe("#/apps/new");
    expect(routeHash({ screen: "keys", applicationId: "north-shop" })).toBe(
      "#/app/north-shop/keys",
    );
    expect(routeHash({ screen: "keys", applicationId: null })).toBe("#/keys");
  });

  it("carries a route's application", () => {
    expect(routeApplication({ screen: "rules", applicationId: "x" })).toBe("x");
    expect(routeApplication({ screen: "channels" })).toBeNull();
  });
});

describe("crumbs (docs/54)", () => {
  const apps = { screen: "apps" } as const;
  const home = { screen: "home" } as const;
  const words = (route: Route, landing: Route = apps) =>
    crumbs(route, "north", landing).map((c) => c.label);

  it("names Home after what it shows, and every other place by its section", () => {
    expect(words(home)).toEqual(["north", "Applications"]);
    expect(words(home, home)).toEqual(["Home"]);
    expect(words({ screen: "unknown" }, home)).toEqual(["Home"]);
    expect(words({ screen: "monitor", tab: "latency" })).toEqual(["Monitoring", "Latency"]);
    expect(words({ screen: "organization" })).toEqual(["north", "Organization settings"]);
    expect(words({ screen: "channels" })).toEqual(["north", "Channels"]);
    expect(words({ screen: "apps-new" })).toEqual(["north", "Applications", "Add application"]);
    expect(words({ screen: "keys", applicationId: "north-shop" })).toEqual([
      "north",
      "API keys",
      "north-shop",
    ]);
    expect(
      words({ screen: "incidents", applicationId: "north-shop", incidentId: "inc_1" }),
    ).toEqual(["north", "Incidents", "north-shop", "inc_1"]);
  });

  it("links every step but the last, which is where the session is", () => {
    for (const route of EVERY) {
      const trail = crumbs(route, "north", apps);
      expect(trail.at(-1)?.route).toBeNull();
      // Each link goes somewhere the page knows.
      for (const crumb of trail.slice(0, -1)) {
        if (crumb.route !== null) expect(parseRoute(routeHash(crumb.route))).toEqual(crumb.route);
      }
    }
  });
});
