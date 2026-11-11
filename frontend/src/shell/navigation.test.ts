import type { Capabilities } from "../console/types";
import { capabilities } from "../test/fixtures";
import { roleText, roleWords } from "./Header";
import { initials } from "./UserMenu";
import {
  allows,
  itemOf,
  landing,
  NAVIGATION,
  namedWith,
  opens,
  target,
  type ItemId,
} from "./navigation";

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

  it("leaves one application's id to the gateway: the item decides", () => {
    // olive can name north-blog only; north-shop is the screen's to ask about.
    expect(opens({ screen: "application", applicationId: "north-shop" }, capabilities.olive)).toBe(
      true,
    );
  });

  it("sends Incidents to the organization's list, or to the first application it may read", () => {
    const home = { screen: "home" } as const;
    expect(target(item("incidents"), home, capabilities.alice)).toEqual({
      screen: "incidents",
      applicationId: null,
      incidentId: null,
    });
    expect(target(item("incidents"), home, capabilities.olive)).toEqual({
      screen: "incidents",
      applicationId: "north-blog",
      incidentId: null,
    });
  });

  it("carries the application in view from one screen to the next", () => {
    const rules = { screen: "rules", applicationId: "north-shop" } as const;
    expect(target(item("application"), rules, capabilities.alice)).toEqual({
      screen: "application",
      applicationId: "north-shop",
    });
    expect(target(item("monitor"), rules, capabilities.root)).toEqual({
      screen: "monitor",
      tab: "overview",
    });
  });
});

describe("applications (docs/51)", () => {
  it("lands a session on its applications only when the answer allows the list", () => {
    expect(landing(only({ organization: { "applications.list": true } }))).toEqual({
      screen: "apps",
    });
    expect(landing(only({ organization: { "applications.list": false } }))).toEqual({
      screen: "home",
    });
    expect(landing(only({ platform: { "monitoring.read": true } }))).toEqual({ screen: "home" });
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

  it("opens the keys only with an application whose keys' list is allowed", () => {
    expect(opens({ screen: "keys", applicationId: null }, capabilities.nina)).toBe(true);
    expect(opens({ screen: "keys", applicationId: null }, capabilities.olive)).toBe(false);
    const rules = { screen: "rules", applicationId: "north-shop" } as const;
    expect(target(item("keys"), rules, capabilities.alice)).toEqual({
      screen: "keys",
      applicationId: "north-shop",
    });
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
