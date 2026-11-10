import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { TimeChartProps } from "../charts/TimeChart";
import type { Capabilities } from "../console/types";
import { ThemeProvider } from "../hooks/useTheme";
import { FakeEventSource, installFakeEventSource } from "../test/fakeEventSource";
import { capabilities, controlSimulated, simulated, type CapabilityUser } from "../test/fixtures";
import { NAVIGATION } from "./navigation";
import { NOT_AVAILABLE, SESSION_ENDED, Shell } from "./Shell";

vi.mock("../charts/TimeChart", () => ({
  TimeChart: (props: TimeChartProps) => <div data-testid="chart" data-unit={props.unit} />,
}));

const PASSWORD = "planted-password-5e1d";
const NEW_PASSWORD = "planted-new-password-0b7c";
const CSRF = "planted-csrf-77a4";

interface Seen {
  method: string;
  path: string;
  csrf: string | undefined;
}

type Me = "in" | "out" | "none" | "change";

/**
 * A stand-in for the gateway: who is logged in (`me`), what it says the session may do
 * (`answer`), and an empty answer for every admin read. Records every call.
 */
function gateway(me: Me, answer: Capabilities = capabilities.alice) {
  const seen: Seen[] = [];
  let state = me;
  const user = () => ({
    user_id: "usr_1",
    username: "someone",
    organization_id: "north",
    must_change_password: state === "change",
  });
  const json = (status: number, body: unknown) =>
    Promise.resolve(new Response(JSON.stringify(body), { status }));
  const refuse = (status: number, code: string) =>
    json(status, { error: { code, message: `refused: ${code}` } });

  const fetcher = (path: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const headers = (init?.headers ?? {}) as Record<string, string>;
    seen.push({ method, path, csrf: headers["X-CSRF-Token"] });
    const body: unknown = typeof init?.body === "string" ? JSON.parse(init.body) : undefined;
    const key = `${method} ${path.split("?")[0] ?? ""}`;
    switch (key) {
      case "GET /v1/auth/me":
        if (state === "none") return json(404, { detail: "Not Found" });
        if (state === "out") return refuse(401, "session_required");
        return json(200, { user: user(), csrf_token: CSRF, step_up_until_ms: null });
      case "POST /v1/auth/login": {
        if ((body as { password: string }).password !== PASSWORD) {
          return refuse(401, "invalid_credentials");
        }
        if (state === "out") state = "in";
        return json(200, { user: user(), csrf_token: CSRF });
      }
      case "POST /v1/auth/change-password":
        state = "in";
        return json(200, { changed: true, sessions_revoked: 0 });
      case "POST /v1/auth/logout":
        state = "out";
        return json(200, { logged_out: true });
      case "GET /v1/auth/capabilities":
        if (state === "change") return refuse(403, "password_change_required");
        if (state !== "in") return refuse(401, "session_required");
        return json(200, answer);
      case "GET /healthz":
        return json(200, { status: "ok", alert_evaluator: "ok" });
      default:
        if (state !== "in") return refuse(401, "session_required");
        if (method === "GET" && path.includes("/incidents")) {
          return json(200, { incidents: [], next: null });
        }
        if (method === "GET" && path.endsWith("/applications?limit=100")) {
          return json(200, { applications: [], next: null });
        }
        return refuse(404, "not_found");
    }
  };
  return { fetcher: fetcher as unknown as typeof fetch, seen, end: () => (state = "out") };
}

function open(fetcher: typeof fetch) {
  render(
    <ThemeProvider>
      <Shell fetcher={fetcher} />
    </ThemeProvider>,
  );
  return userEvent.setup();
}

async function logIn(user: ReturnType<typeof userEvent.setup>) {
  const form = await screen.findByRole("region", { name: "Log in" });
  await user.type(within(form).getByLabelText("Username"), "someone");
  await user.type(within(form).getByLabelText("Password"), PASSWORD);
  await user.click(within(form).getByRole("button", { name: "Log in" }));
}

async function sections(): Promise<string[]> {
  const nav = await screen.findByRole("navigation", { name: "Sections" });
  return within(nav)
    .getAllByRole("link")
    .map((link) => link.textContent);
}

/** What a fixture says, read here on its own: the test holds no table of who may do what. */
function says(answer: Capabilities, level: string, action: string): boolean {
  if (level === "application") return answer.applications.some((a) => a.actions[action] === true);
  if (level === "platform") return answer.platform[action] === true;
  return answer.organization[action] === true;
}

function dump(storage: Storage): string {
  return Array.from({ length: storage.length }, (_, i) => {
    const key = storage.key(i) ?? "";
    return `${key}=${storage.getItem(key) ?? ""}`;
  }).join(String.fromCharCode(10));
}

describe("navigation", () => {
  it.each(Object.entries(capabilities))(
    "shows exactly the items the gateway's answer allows (%s)",
    async (_name, answer) => {
      const { fetcher } = gateway("in", answer);
      open(fetcher);
      const expected = NAVIGATION.filter(
        (item) =>
          item.asks.length === 0 || item.asks.some((ask) => says(answer, ask.level, ask.action)),
      ).map((item) => item.label);
      expect(await sections()).toEqual(expected);
      // Every item, shown or hidden, is decided: Home is always there.
      expect(expected[0]).toBe("Home");
    },
  );

  it("draws no item at all before the gateway has said what the session may do", async () => {
    const { fetcher } = gateway("out");
    open(fetcher);
    await screen.findByRole("region", { name: "Log in" });
    expect(screen.queryByRole("navigation", { name: "Sections" })).toBeNull();
  });

  it("asks again what the session may do at each new section", async () => {
    const { fetcher, seen } = gateway("in");
    const user = open(fetcher);
    const nav = await screen.findByRole("navigation", { name: "Sections" });
    const asked = () => seen.filter((s) => s.path === "/v1/auth/capabilities").length;
    const before = asked();
    await user.click(within(nav).getByRole("link", { name: "Channels" }));
    await waitFor(() => {
      expect(asked()).toBe(before + 1);
    });
  });
});

describe("places the session may not open", () => {
  it.each<[CapabilityUser, string, string]>([
    ["bob", "#/channels", "/alert-channels"],
    ["olive", "#/organization", "/settings"],
    ["root", "#/rules/north-shop", "/alert-rules"],
    ["vera", "#/monitor/overview", "/v1/monitor"],
  ])("%s on %s gets home with one sentence and no request for it", async (name, hash, path) => {
    window.location.hash = hash;
    const { fetcher, seen } = gateway("in", capabilities[name]);
    open(fetcher);
    const home = await screen.findByRole("region", { name: "Home" });
    expect(within(home).getByRole("alert")).toHaveTextContent(NOT_AVAILABLE);
    expect(seen.some((s) => s.path.includes(path))).toBe(false);
    expect(window.location.hash).toBe(hash);
  });

  it("answers a place the page does not know with the same sentence", async () => {
    window.location.hash = "#/nowhere/at-all";
    const { fetcher } = gateway("in");
    open(fetcher);
    const home = await screen.findByRole("region", { name: "Home" });
    expect(within(home).getByRole("alert")).toHaveTextContent(NOT_AVAILABLE);
  });
});

describe("home", () => {
  it("has a platform section for a platform answer, and says who manages organizations", async () => {
    const { fetcher } = gateway("in", capabilities.root);
    open(fetcher);
    const home = await screen.findByRole("region", { name: "Home" });
    expect(within(home).getByRole("region", { name: "Platform" })).toHaveTextContent(
      "managed by that organization's own administrators",
    );
    expect(within(home).getByRole("link", { name: "Monitoring" })).toBeInTheDocument();
  });

  it("has an organization section with the open incidents, and says per-organization monitoring is not built", async () => {
    const { fetcher, seen } = gateway("in", capabilities.alice);
    open(fetcher);
    const section = await screen.findByRole("region", { name: "Organization north" });
    expect(await within(section).findByText("Open incidents:")).toBeInTheDocument();
    expect(section).toHaveTextContent("Monitoring for one organization is not built yet");
    for (const state of ["pending", "firing", "acknowledged"]) {
      expect(seen.some((s) => s.path.includes(`/incidents?state=${state}`))).toBe(true);
    }
    expect(screen.queryByRole("region", { name: "Platform" })).toBeNull();
  });

  it("lists an application-only session's applications with their links", async () => {
    const { fetcher } = gateway("in", capabilities.olive);
    open(fetcher);
    const section = await screen.findByRole("region", { name: "Your applications" });
    expect(section).toHaveTextContent("north-blog");
    expect(within(section).getByRole("link", { name: "Settings" })).toHaveAttribute(
      "href",
      "#/application/north-blog",
    );
    expect(screen.queryByRole("region", { name: /^Organization/ })).toBeNull();
  });

  it("says there is nothing yet, with Log out, to a session the gateway allows nothing", async () => {
    const { fetcher } = gateway("in", capabilities.bob);
    open(fetcher);
    const section = await screen.findByRole("region", { name: "No access yet" });
    expect(section).toHaveTextContent(
      "This account has no access to any part of the gateway yet. Ask your organization's administrator.",
    );
    expect(within(section).getByRole("button", { name: "Log out" })).toBeInTheDocument();
  });

  it("shows who is logged in, the organization and the roles the gateway names", async () => {
    const { fetcher } = gateway("in", capabilities.olive);
    open(fetcher);
    await screen.findByRole("region", { name: "Home" });
    // The role as the gateway named it, read from its own answer.
    const [held] = capabilities.olive.roles;
    expect(screen.getByLabelText("Logged in as")).toHaveTextContent(
      `someone · organization north · ${held?.role ?? "?"} (application north-blog)`,
    );
  });
});

describe("sessions", () => {
  it("logs in from a deep link and opens it", async () => {
    window.location.hash = "#/rules/north-shop";
    const { fetcher, seen } = gateway("out");
    const user = open(fetcher);
    await logIn(user);
    await screen.findByRole("heading", { name: "Alert rules", level: 2 });
    expect(window.location.hash).toBe("#/rules/north-shop");
    expect(seen.some((s) => s.path.includes("/applications/north-shop/alert-rules"))).toBe(true);
  });

  it("asks only for a new password, and nothing else, until it is changed", async () => {
    const { fetcher, seen } = gateway("change");
    const user = open(fetcher);
    const change = await screen.findByRole("region", { name: "Change your password" });
    expect(screen.queryByRole("navigation", { name: "Sections" })).toBeNull();
    expect(seen.some((s) => s.path === "/v1/auth/capabilities")).toBe(false);
    await user.type(within(change).getByLabelText("Current password"), PASSWORD);
    await user.type(within(change).getByLabelText("New password"), NEW_PASSWORD);
    await user.click(within(change).getByRole("button", { name: "Change password" }));
    await screen.findByRole("navigation", { name: "Sections" });
    expect(seen.some((s) => s.path === "/v1/auth/capabilities")).toBe(true);
  });

  it("logs in again where it was when the session ends, and sends no old token", async () => {
    window.location.hash = "#/incidents";
    const { fetcher, seen, end } = gateway("in");
    const user = open(fetcher);
    const nav = await screen.findByRole("navigation", { name: "Sections" });
    end();
    // The next section's call is answered 401 session_required.
    await user.click(within(nav).getByRole("link", { name: "Channels" }));
    const login = await screen.findByRole("region", { name: "Log in" });
    expect(login).toHaveTextContent(SESSION_ENDED);
    expect(window.location.hash).toBe("#/channels");
    await logIn(user);
    await screen.findByRole("heading", { name: "Channels", level: 2 });
    const loggedIn = seen.filter((s) => s.path === "/v1/auth/login");
    expect(loggedIn.at(-1)?.csrf).toBeUndefined();
  });

  it("logs out to home, forgets the token, and keeps nothing in storage", async () => {
    window.location.hash = "#/channels";
    const { fetcher, seen } = gateway("out");
    const user = open(fetcher);
    await logIn(user);
    await screen.findByRole("heading", { name: "Channels", level: 2 });
    await user.click(screen.getByRole("button", { name: "Log out" }));
    await screen.findByText("Logged out.");
    expect(window.location.hash).toBe("#/home");
    expect(seen.find((s) => s.path === "/v1/auth/logout")?.csrf).toBe(CSRF);
    await logIn(user);
    await screen.findByRole("region", { name: "Home" });
    expect(seen.filter((s) => s.path === "/v1/auth/login").at(-1)?.csrf).toBeUndefined();
    const stored = dump(localStorage) + dump(sessionStorage);
    expect(stored).not.toContain(CSRF);
    expect(stored).not.toContain(PASSWORD);
    expect(document.body.innerHTML).not.toContain(CSRF);
  });
});

describe("without accounts (simulated live mode)", () => {
  function simulatedLive() {
    installFakeEventSource();
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify(controlSimulated)))),
    );
    return gateway("none");
  }

  it("shows the monitoring, labelled simulated, with no login and no console item", async () => {
    const { fetcher } = simulatedLive();
    open(fetcher);
    await screen.findByRole("tab", { name: "Overview" });
    FakeEventSource.latest.push("snapshot", simulated);
    expect(await screen.findByRole("status", { name: "Simulated live mode" })).toBeInTheDocument();
    expect(await screen.findByText("Simulated")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Log in" })).toBeNull();
    expect(screen.queryByRole("navigation", { name: "Sections" })).toBeNull();
  });

  it.each(["#/organization", "#/rules/app-one", "#/nowhere"])(
    "answers %s with 'This mode has no accounts', not a login",
    async (hash) => {
      window.location.hash = hash;
      const { fetcher } = simulatedLive();
      open(fetcher);
      const panel = await screen.findByRole("region", { name: "This mode has no accounts" });
      expect(within(panel).getByRole("link", { name: "Open the monitoring" })).toHaveAttribute(
        "href",
        "#/monitor/overview",
      );
      expect(screen.queryByRole("region", { name: "Log in" })).toBeNull();
    },
  );
});
