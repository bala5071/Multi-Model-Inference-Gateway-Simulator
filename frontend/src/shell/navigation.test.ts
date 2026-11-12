import type { Capabilities } from "../console/types";
import { capabilities } from "../test/fixtures";
import { roleText, roleWords } from "./Header";
import { initials } from "./UserMenu";
import {
  allows,
  APP_TAB_ACTIONS,
  appLinks,
  appTabs,
  isShown,
  itemOf,
  landing,
  NAVIGATION,
  namedWith,
  opens,
  target,
  type ItemId,
} from "./navigation";
import type { APP_TABS } from "./routes";

const page = (applicationId: string, tab: (typeof APP_TABS)[number]) =>
  ({ screen: "app", applicationId, tab, incidentId: null }) as const;

function item(id: ItemId) {
  const found = NAVIGATION.find((candidate) => candidate.id === id);
  if (found === undefined) throw new Error(id);
  return found;
}

/** An answer with nothing allowed but what is given. */
function only(given: Partial<Capabilities>): Capabilities {
  return {
    roles: [],
    platform: {},
    organization: {},
    applications: [],
    more_applications: false,
    ...given,
  };
}

describe("navigation", () => {
  it("reads an application action as allowed when any named application allows it", () => {
    expect(allows(capabilities.olive, { level: "application", action: "settings.read" })).toBe(
      true,
    );
    expect(
      allows(capabilities.olive, { level: "application", action: "application_keys.list" }),
    ).toBe(false);
    expect(namedWith(capabilities.alice, "settings.read")).toEqual(["north-blog", "north-shop"]);
  });

  it("treats an action the answer does not mention as refused", () => {
    expect(allows(only({}), { level: "platform", action: "monitoring.read" })).toBe(false);
    expect(opens({ screen: "channels" }, only({}))).toBe(false);
    expect(opens({ screen: "home" }, only({}))).toBe(true);
    expect(opens(page("north-shop", "overview"), only({}))).toBe(false);
  });

  it("opens nothing the page does not know", () => {
    expect(itemOf({ screen: "unknown" })).toBeNull();
    expect(opens({ screen: "unknown" }, capabilities.alice)).toBe(false);
  });

  it("opens the Playground tab only when the Playground is allowed", () => {
    const monitoring = only({ platform: { "monitoring.read": true } });
    expect(opens({ screen: "monitor", tab: "latency" }, monitoring)).toBe(true);
    expect(opens({ screen: "monitor", tab: "playground" }, monitoring)).toBe(false);
    expect(opens({ screen: "monitor", tab: "playground" }, capabilities.root)).toBe(true);
  });

  it("sends Incidents to the organization's list, and keeps it for the organization's sessions", () => {
    expect(target(item("incidents"), { screen: "home" })).toEqual({ screen: "incidents" });
    expect(isShown(item("incidents"), capabilities.alice)).toBe(true);
    // An application's own sessions read their incidents on its page (docs/56).
    expect(isShown(item("incidents"), capabilities.olive)).toBe(false);
    expect(opens({ screen: "incidents" }, capabilities.olive)).toBe(false);
  });

  it("keeps the monitoring's tab when it is already open", () => {
    const latency = { screen: "monitor", tab: "latency" } as const;
    expect(target(item("monitor"), latency)).toEqual(latency);
    expect(target(item("monitor"), page("x", "rules"))).toEqual({
      screen: "monitor",
      tab: "overview",
    });
  });
});

describe("the application page (docs/56)", () => {
  it("shows the tabs the gateway allowed on that application, in their order", () => {
    expect(appTabs(capabilities.nina, "north-shop")).toEqual([
      "overview",
      "settings",
      "rules",
      "incidents",
      "keys",
    ]);
    // olive reads north-blog, without its keys.
    expect(appTabs(capabilities.olive, "north-blog")).toEqual([
      "overview",
      "settings",
      "rules",
      "incidents",
    ]);
    // Each tab follows its own action.
    for (const [tab, action] of Object.entries(APP_TAB_ACTIONS)) {
      const without = only({
        applications: [
          {
            application_id: "a",
            actions: Object.fromEntries(
              Object.values(APP_TAB_ACTIONS).map((each) => [each, each !== action]),
            ),
          },
        ],
      });
      expect(appTabs(without, "a")).not.toContain(tab);
      expect(opens(page("a", tab as (typeof APP_TABS)[number]), without)).toBe(false);
    }
  });

  it("leaves an application the answer did not name to the gateway: any named one's tabs", () => {
    // olive names north-blog only; north-shop is the page's to ask about, with olive's tabs.
    expect(appTabs(capabilities.olive, "north-shop")).toEqual(
      appTabs(capabilities.olive, "north-blog"),
    );
    expect(opens(page("north-shop", "settings"), capabilities.olive)).toBe(true);
    expect(opens(page("north-shop", "keys"), capabilities.olive)).toBe(false);
  });

  it("names the applications in the sidebar only for a session that cannot list them", () => {
    expect(appLinks(capabilities.nina)).toEqual(["north-shop"]);
    expect(appLinks(capabilities.olive)).toEqual(["north-blog"]);
    expect(appLinks(capabilities.alice)).toEqual([]);
    expect(appLinks(capabilities.root)).toEqual([]);
  });

  it("opens an address with no application only where some application allows its tab", () => {
    const none = (tab: (typeof APP_TABS)[number]) =>
      ({ screen: "app", applicationId: null, tab, incidentId: null }) as const;
    expect(opens(none("settings"), capabilities.olive)).toBe(true);
    expect(opens(none("keys"), capabilities.olive)).toBe(false);
    expect(opens(none("keys"), capabilities.nina)).toBe(true);
    expect(opens(none("overview"), only({}))).toBe(false);
  });
});

describe("applications (docs/51)", () => {
  it("lands a session as console-ia-v2-plan 2.1 says", () => {
    expect(landing(only({ organization: { "applications.list": true } }))).toEqual({
      screen: "apps",
    });
    expect(landing(only({ organization: { "applications.list": false } }))).toEqual({
      screen: "home",
    });
    expect(landing(only({ platform: { "monitoring.read": true } }))).toEqual({ screen: "home" });
    // One application: its page.
    expect(landing(capabilities.nina)).toEqual(page("north-shop", "overview"));
    expect(landing(capabilities.alice)).toEqual({ screen: "apps" });
    // Several, without the list: Home, which lists them.
    const several = only({
      applications: [
        { application_id: "a", actions: { "applications.read": true } },
        { application_id: "b", actions: { "applications.read": true } },
      ],
    });
    expect(landing(several)).toEqual({ screen: "home" });
  });

  it("opens the Add form only when the answer allows creating an application", () => {
    const listing = only({ organization: { "applications.list": true } });
    expect(opens({ screen: "apps" }, listing)).toBe(true);
    expect(opens({ screen: "apps-new" }, listing)).toBe(false);
    const creating = only({
      organization: { "applications.list": true, "applications.create": true },
    });
    expect(opens({ screen: "apps-new" }, creating)).toBe(true);
    expect(itemOf({ screen: "apps-new" })).toBe("apps");
  });
});

describe("initials (docs/54)", () => {
  it("takes up to two letters for the avatar", () => {
    expect(initials("admin-e2e")).toBe("AE");
    expect(initials("someone")).toBe("SO");
    expect(initials("x")).toBe("X");
  });
});

describe("roles as words (docs/54)", () => {
  it("writes the gateway's role name with spaces for underscores, and keeps its scope", () => {
    expect(roleWords("a_b_c")).toBe("a b c");
    expect(roleWords("plain")).toBe("plain");
    expect(roleText({ role: "x_y", scope_type: "organization", scope_id: "acme" })).toBe(
      "x y (organization acme)",
    );
    expect(roleText({ role: "x_y", scope_type: "platform", scope_id: "" })).toBe("x y");
  });
});
