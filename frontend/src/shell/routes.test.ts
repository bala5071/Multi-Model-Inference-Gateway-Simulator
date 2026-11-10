import { MONITOR_TABS, parseRoute, routeApplication, routeHash, type Route } from "./routes";

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
    ]) {
      expect(parseRoute(hash)).toEqual({ screen: "unknown" });
    }
    expect(routeHash({ screen: "unknown" })).toBe("#/home");
  });

  it("carries a route's application", () => {
    expect(routeApplication({ screen: "rules", applicationId: "x" })).toBe("x");
    expect(routeApplication({ screen: "channels" })).toBeNull();
  });
});
