import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { TimeChartProps } from "../charts/TimeChart";
import type { Capabilities } from "../console/types";
import { ThemeProvider } from "../hooks/useTheme";
import { FakeEventSource, installFakeEventSource } from "../test/fakeEventSource";
import {
  applicationsAlice,
  capabilities,
  controlSimulated,
  incidentCountsAlice,
  keysAlice,
  metricsAlice,
  plansAlice,
  simulated,
  type CapabilityUser,
} from "../test/fixtures";
import { TOO_FEW, TOO_FEW_EXPLAINED, TOO_FEW_WORDS } from "../console/applications";
import { LOCKOUT_SENTENCE, PASSWORD_RULES } from "../console/passwordRules";
import { ToastProvider, Toasted } from "../ui/Toasts";
import { roleWords } from "./Header";
import { NAVIGATION } from "./navigation";
import { NOT_AVAILABLE, SESSION_ENDED, Shell } from "./Shell";

vi.mock("../charts/TimeChart", () => ({
  TimeChart: (props: TimeChartProps) => <div data-testid="chart" data-unit={props.unit} />,
}));

const PASSWORD = "planted-password-5e1d";
const NEW_PASSWORD = "planted-new-password-0b7c";
const CSRF = "planted-csrf-77a4";
// What the fake gateway answers as a new key: planted, so the tests can look for it anywhere.
const PLANTED_KEY = "planted-api-key-3c9e";

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
  // Whether this session has given its password again (step-up), for the key writes.
  let steppedUp = false;
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
      case "POST /v1/auth/step-up":
        if (state !== "in") return refuse(401, "session_required");
        if ((body as { password: string }).password !== PASSWORD) {
          return refuse(403, "step_up_failed");
        }
        steppedUp = true;
        return json(200, { step_up_until_ms: 1 });
      case "GET /v1/admin/orgs/north/applications":
        if (state !== "in") return refuse(401, "session_required");
        return json(
          200,
          path.endsWith("?limit=100") ? { applications: [], next: null } : applicationsAlice,
        );
      case "GET /v1/admin/orgs/north/metrics":
        if (state !== "in") return refuse(401, "session_required");
        return json(200, metricsAlice);
      case "GET /v1/admin/orgs/north/incidents/counts":
        if (state !== "in") return refuse(401, "session_required");
        return json(200, incidentCountsAlice);
      case "GET /v1/admin/orgs/north/plans":
        if (state !== "in") return refuse(401, "session_required");
        return json(200, plansAlice);
      case "POST /v1/admin/orgs/north/applications": {
        if (state !== "in") return refuse(401, "session_required");
        const { name, plan } = body as { name: string; plan: string };
        const listed = applicationsAlice.applications[0];
        return json(201, { ...listed, application_id: "app_new1", name, plan });
      }
      default:
        if (state !== "in") return refuse(401, "session_required");
        if (/\/applications\/[^/]+\/keys/.test(path)) {
          if (method === "GET") return json(200, keysAlice);
          if (!steppedUp) return refuse(403, "step_up_required");
          if (path.endsWith("/revoke")) return json(200, { key_id: "k", state: "revoked" });
          const label = (body as { label?: string } | undefined)?.label ?? "rotated";
          return json(201, { ...keysAlice.keys[0], label, key: PLANTED_KEY });
        }
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

/** The region a session lands on, read from its own answer: Applications when it may list them. */
function landingName(answer: Capabilities): string {
  return answer.organization["applications.list"] === true ? "Applications" : "Home";
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
    const home = await screen.findByRole("region", { name: landingName(capabilities[name]) });
    expect(within(home).getByRole("alert")).toHaveTextContent(NOT_AVAILABLE);
    expect(seen.some((s) => s.path.includes(path))).toBe(false);
    expect(window.location.hash).toBe(hash);
  });

  it.each<[CapabilityUser, string, string]>([
    ["vera", "#/apps/new", "/plans"],
    ["nina", "#/apps", "/metrics"],
    ["nina", "#/apps/new", "/plans"],
    ["olive", "#/keys", "/keys"],
    ["root", "#/apps", "/applications?limit=50"],
  ])(
    "%s on %s (docs/51) lands with one sentence and no request for it",
    async (name, hash, path) => {
      window.location.hash = hash;
      const { fetcher, seen } = gateway("in", capabilities[name]);
      open(fetcher);
      const landed = await screen.findByRole("region", { name: landingName(capabilities[name]) });
      expect(within(landed).getAllByRole("alert")[0]).toHaveTextContent(NOT_AVAILABLE);
      expect(seen.some((s) => s.path.includes(path))).toBe(false);
    },
  );

  it("answers a place the page does not know with the same sentence", async () => {
    window.location.hash = "#/nowhere/at-all";
    const { fetcher } = gateway("in");
    open(fetcher);
    const landed = await screen.findByRole("region", { name: landingName(capabilities.alice) });
    expect(within(landed).getByRole("alert")).toHaveTextContent(NOT_AVAILABLE);
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
    // An answer with organization actions but not the list: such a session keeps Home.
    const answer: Capabilities = {
      ...capabilities.vera,
      organization: { ...capabilities.vera.organization, "applications.list": false },
    };
    const { fetcher, seen } = gateway("in", answer);
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

  it("shows who is logged in, the organization and the roles the gateway names, in the account menu", async () => {
    const { fetcher } = gateway("in", capabilities.olive);
    const user = open(fetcher);
    await screen.findByRole("region", { name: "Home" });
    const button = screen.getByRole("button", { name: "Account: someone" });
    expect(button).toHaveAttribute("aria-haspopup", "menu");
    expect(button).toHaveAttribute("aria-expanded", "false");
    await user.click(button);
    const menu = screen.getByRole("menu", { name: "Account" });
    // The role the gateway named, read from its own answer, as words.
    const [held] = capabilities.olive.roles;
    const who = document.getElementById(menu.getAttribute("aria-describedby") ?? "");
    expect(who).not.toBeNull();
    expect(who).toHaveTextContent("someone");
    expect(who).toHaveTextContent("Organization north");
    expect(who).toHaveTextContent(`${roleWords(held?.role ?? "?")} (application north-blog)`);
    expect(
      within(menu)
        .getAllByRole("menuitemradio")
        .map((item) => item.textContent),
    ).toEqual(["System", "Light", "Dark"]);
    expect(within(menu).getByRole("menuitem", { name: "Log out" })).toBeInTheDocument();
  });

  it("works the account menu by keyboard: Enter opens it, arrows move, Escape gives the focus back", async () => {
    const { fetcher } = gateway("in", capabilities.olive);
    const user = open(fetcher);
    await screen.findByRole("region", { name: "Home" });
    const button = screen.getByRole("button", { name: "Account: someone" });
    button.focus();
    await user.keyboard("{Enter}");
    const items = [
      ...screen.getAllByRole("menuitemradio"),
      screen.getByRole("menuitem", { name: "Log out" }),
    ];
    expect(items[0]).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(items[1]).toHaveFocus();
    await user.keyboard("{End}");
    expect(items[3]).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(items[0]).toHaveFocus();
    await user.keyboard("{ArrowUp}");
    expect(items[3]).toHaveFocus();
    await user.keyboard("{Home}");
    expect(items[0]).toHaveFocus();
    // Choosing Dark from the menu applies it at once.
    await user.keyboard("{ArrowDown}{ArrowDown}{Enter}");
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(screen.getByRole("menuitemradio", { name: "Dark" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).toBeNull();
    expect(button).toHaveFocus();
    expect(button).toHaveAttribute("aria-expanded", "false");
    // ArrowUp on the button opens it on the last item.
    await user.keyboard("{ArrowUp}");
    expect(screen.getByRole("menuitem", { name: "Log out" })).toHaveFocus();
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
    await user.click(screen.getByRole("button", { name: "Account: someone" }));
    await user.click(screen.getByRole("menuitem", { name: "Log out" }));
    await screen.findByText("Logged out.");
    expect(window.location.hash).toBe("#/home");
    expect(seen.find((s) => s.path === "/v1/auth/logout")?.csrf).toBe(CSRF);
    await logIn(user);
    await screen.findByRole("region", { name: landingName(capabilities.alice) });
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

describe("applications (docs/51)", () => {
  it("lands a session that may list applications on them, figures over the last 60 minutes", async () => {
    const { fetcher, seen } = gateway("in", capabilities.alice);
    open(fetcher);
    const list = await screen.findByRole("region", { name: "Applications" });
    const table = await within(list).findByRole("table", { name: "Applications" });
    expect(within(table).getAllByRole("row")).toHaveLength(
      applicationsAlice.applications.length + 1,
    );
    expect(list).toHaveTextContent("Figures over the last 60 minutes.");
    const shop = within(table).getByRole("row", { name: /north-shop/ });
    expect(shop).toHaveTextContent("20.0 %");
    // Figures in monospace (docs/52, Q1): the error rate, and the application's id.
    expect(within(shop).getByText("20.0 %")).toHaveClass("figure");
    expect(within(shop).getByText("(north-shop)")).toHaveClass("figure");
    expect(shop).toHaveTextContent("firing");
    expect(within(shop).getByRole("link", { name: "API keys" })).toHaveAttribute(
      "href",
      "#/app/north-shop/keys",
    );
    const nav = await screen.findByRole("navigation", { name: "Sections" });
    expect(within(nav).getByRole("link", { name: "Applications" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    // The control appears because the answer allows creating one, read from the fixture.
    expect(capabilities.alice.organization["applications.create"]).toBe(true);
    expect(within(list).getByRole("link", { name: "Add application" })).toHaveAttribute(
      "href",
      "#/apps/new",
    );
    for (const read of ["/metrics?window=60m", "/incidents/counts", "/healthz"]) {
      expect(seen.some((s) => s.path.includes(read))).toBe(true);
    }
  });

  it("shows a session the answer does not allow to create no Add application, and no keys", async () => {
    expect(capabilities.vera.organization["applications.create"]).toBe(false);
    const { fetcher } = gateway("in", capabilities.vera);
    open(fetcher);
    const list = await screen.findByRole("region", { name: "Applications" });
    await within(list).findByRole("table", { name: "Applications" });
    expect(within(list).queryByRole("link", { name: "Add application" })).toBeNull();
    expect(within(list).queryByRole("link", { name: "API keys" })).toBeNull();
  });

  it("logs in from a deep link to the Add form and opens it", async () => {
    window.location.hash = "#/apps/new";
    const { fetcher, seen } = gateway("out");
    const user = open(fetcher);
    await logIn(user);
    await screen.findByRole("heading", { name: "Add application", level: 2 });
    expect(window.location.hash).toBe("#/apps/new");
    await waitFor(() => {
      expect(seen.some((s) => s.path.endsWith("/plans"))).toBe(true);
    });
  });

  it("creates an application, asks the password for its first key, shows it once, then forgets it", async () => {
    window.location.hash = "#/apps/new";
    const log = vi.spyOn(console, "log");
    const { fetcher, seen } = gateway("in");
    const user = open(fetcher);
    const form = await screen.findByRole("region", { name: "Add application" });
    await user.type(await within(form).findByLabelText("Name"), "Fresh");
    const [plan] = plansAlice.plans;
    await user.selectOptions(within(form).getByLabelText("Plan"), plan?.name ?? "");
    await user.click(within(form).getByRole("button", { name: "Create" }));
    await within(form).findByText(/Created/);
    expect(seen.find((s) => s.method === "POST" && s.path.endsWith("/applications"))?.csrf).toBe(
      CSRF,
    );
    await user.click(within(form).getByRole("button", { name: "Issue the first key" }));
    // The gateway asks for a step-up: the password, once, then the key.
    const dialog = await screen.findByRole("dialog", { name: "Confirm with your password" });
    await user.type(within(dialog).getByLabelText("Password"), PASSWORD);
    await user.click(within(dialog).getByRole("button", { name: "Confirm" }));
    const shown = await screen.findByRole("region", { name: "New API key" });
    expect(shown).toHaveTextContent("Shown once. It cannot be shown again.");
    expect(within(shown).getByLabelText("API key")).toHaveValue(PLANTED_KEY);
    // Never in storage, the address, or the console log.
    const stored = dump(localStorage) + dump(sessionStorage);
    expect(stored).not.toContain(PLANTED_KEY);
    expect(window.location.href).not.toContain(PLANTED_KEY);
    expect(log.mock.calls.flat().join(" ")).not.toContain(PLANTED_KEY);
    await user.click(within(shown).getByRole("button", { name: "I have stored it" }));
    expect(screen.queryByRole("region", { name: "New API key" })).toBeNull();
    expect(document.body.innerHTML).not.toContain(PLANTED_KEY);
    log.mockRestore();
  });

  it("lists an application's keys with their changes, and shows a rotated key once", async () => {
    window.location.hash = "#/app/north-shop/keys";
    const { fetcher } = gateway("in", capabilities.alice);
    const user = open(fetcher);
    const keys = await screen.findByRole("table", { name: "Keys" });
    const active = keysAlice.keys.filter((k) => k.state !== "revoked");
    expect(within(keys).getAllByRole("button", { name: /^Revoke / })).toHaveLength(active.length);
    const [first] = active;
    await user.click(within(keys).getByRole("button", { name: `Rotate ${first?.label ?? ""}` }));
    const dialog = await screen.findByRole("dialog", { name: "Confirm with your password" });
    await user.type(within(dialog).getByLabelText("Password"), PASSWORD);
    await user.click(within(dialog).getByRole("button", { name: "Confirm" }));
    const shown = await screen.findByRole("region", { name: "New API key" });
    expect(within(shown).getByLabelText("API key")).toHaveValue(PLANTED_KEY);
  });

  it("asks before revoking a key, in a dialog whose Cancel comes first and sends nothing", async () => {
    window.location.hash = "#/app/north-shop/keys";
    const { fetcher, seen } = gateway("in", capabilities.alice);
    const user = open(fetcher);
    const keys = await screen.findByRole("table", { name: "Keys" });
    const [first] = keysAlice.keys.filter((k) => k.state !== "revoked");
    const label = first?.label ?? "";
    const revokes = () => seen.filter((s) => s.method === "POST" && s.path.endsWith("/revoke"));

    await user.click(within(keys).getByRole("button", { name: `Revoke ${label}` }));
    let dialog = await screen.findByRole("alertdialog", { name: "Revoke a key" });
    expect(dialog).toHaveTextContent(`Revoke ${label}`);
    expect(dialog).toHaveTextContent("This cannot be undone.");
    const buttons = within(dialog).getAllByRole("button");
    expect(buttons.map((b) => b.textContent)).toEqual(["Cancel", "Revoke"]);
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(revokes()).toEqual([]);

    await user.click(within(keys).getByRole("button", { name: `Revoke ${label}` }));
    dialog = await screen.findByRole("alertdialog", { name: "Revoke a key" });
    await user.click(within(dialog).getByRole("button", { name: "Revoke" }));
    // The confirmation closed; the gateway still asks for the password, in its own dialog.
    const stepUp = await screen.findByRole("dialog", { name: "Confirm with your password" });
    await user.type(within(stepUp).getByLabelText("Password"), PASSWORD);
    await user.click(within(stepUp).getByRole("button", { name: "Confirm" }));
    await waitFor(() => {
      expect(revokes().length).toBeGreaterThan(0);
    });
    expect(revokes().every((s) => s.path.includes(`/keys/${first?.key_id ?? ""}/revoke`))).toBe(
      true,
    );
  });

  it("gives an application's own admin its keys from home", async () => {
    const { fetcher } = gateway("in", capabilities.nina);
    open(fetcher);
    const section = await screen.findByRole("region", { name: "Your applications" });
    expect(within(section).getByRole("link", { name: "API keys" })).toHaveAttribute(
      "href",
      "#/app/north-shop/keys",
    );
    // olive may not list north-blog's keys: no link.
    expect(capabilities.olive.applications[0]?.actions["application_keys.list"]).toBe(false);
  });

  it("logs in again where it was when the session ends on the Applications list", async () => {
    window.location.hash = "#/channels";
    const { fetcher, end } = gateway("in");
    const user = open(fetcher);
    const nav = await screen.findByRole("navigation", { name: "Sections" });
    end();
    await user.click(within(nav).getByRole("link", { name: "Applications" }));
    const login = await screen.findByRole("region", { name: "Log in" });
    expect(login).toHaveTextContent(SESSION_ENDED);
    expect(window.location.hash).toBe("#/apps");
  });

  it("logs in again where it was when the session ends on a key write", async () => {
    window.location.hash = "#/app/north-shop/keys";
    const { fetcher, end } = gateway("in");
    const user = open(fetcher);
    const issue = await screen.findByRole("form", { name: "Issue a key" });
    await user.type(within(issue).getByLabelText("Label"), "late");
    end();
    await user.click(within(issue).getByRole("button", { name: "Issue a key" }));
    const login = await screen.findByRole("region", { name: "Log in" });
    expect(login).toHaveTextContent(SESSION_ENDED);
    expect(window.location.hash).toBe("#/app/north-shop/keys");
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("the phone's drawer (docs/54, docs/55)", () => {
  it("closes when a link is followed, the place already shown included", async () => {
    window.location.hash = "#/apps";
    const { fetcher } = gateway("in", capabilities.alice);
    const user = open(fetcher);
    await screen.findByRole("heading", { name: "Applications", level: 2 });
    const drawer = () => document.querySelector<HTMLDialogElement>("dialog.drawer");
    await user.click(screen.getByRole("button", { name: "Open the navigation" }));
    expect(drawer()?.open).toBe(true);
    // The place does not change: the drawer must close all the same.
    await user.click(within(drawer() as HTMLElement).getByRole("link", { name: "Applications" }));
    expect(drawer()?.open).toBe(false);
    await user.click(screen.getByRole("button", { name: "Open the navigation" }));
    await user.click(within(drawer() as HTMLElement).getByRole("link", { name: "Incidents" }));
    expect(drawer()?.open).toBe(false);
    expect(window.location.hash).toBe("#/incidents");
  });
});

describe("the frame (docs/54)", () => {
  it("groups the sidebar by level, keeps each label as its name when collapsed, and keeps only the word", async () => {
    const { fetcher } = gateway("in", capabilities.alice);
    const user = open(fetcher);
    const nav = await screen.findByRole("navigation", { name: "Sections" });
    const labels = within(nav)
      .getAllByRole("link")
      .map((link) => link.textContent);
    await user.click(screen.getByRole("button", { name: "Collapse the sidebar" }));
    expect(screen.getByRole("button", { name: "Expand the sidebar" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    // Collapsed: the same links, by the same names, each with its tooltip.
    const collapsed = within(screen.getByRole("navigation", { name: "Sections" })).getAllByRole(
      "link",
    );
    expect(collapsed.map((link) => link.textContent)).toEqual(labels);
    for (const link of collapsed) expect(link).toHaveAttribute("title", link.textContent);
    expect(localStorage.getItem("gateway-sidebar")).toBe("collapsed");
    await user.click(screen.getByRole("button", { name: "Expand the sidebar" }));
    expect(localStorage.getItem("gateway-sidebar")).toBe("expanded");
  });

  it("shows breadcrumbs for the place, the last one current", async () => {
    window.location.hash = "#/apps/new";
    const { fetcher } = gateway("in", capabilities.alice);
    open(fetcher);
    const trail = await screen.findByRole("navigation", { name: "Breadcrumb" });
    expect(
      within(trail)
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual(["north", "Applications", "Add application"]);
    expect(within(trail).getByRole("link", { name: "Applications" })).toHaveAttribute(
      "href",
      "#/apps",
    );
    expect(within(trail).getByText("Add application")).toHaveAttribute("aria-current", "page");
  });

  it("skips to the content with the first control of the page", async () => {
    const { fetcher } = gateway("in", capabilities.alice);
    const user = open(fetcher);
    await screen.findByRole("navigation", { name: "Sections" });
    await user.tab();
    const skip = screen.getByRole("button", { name: "Skip to content" });
    expect(skip).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(document.activeElement).toHaveClass("frame-content");
  });

  it("shows a change's result as a toast that can be dismissed", async () => {
    const user = userEvent.setup();
    render(
      <ToastProvider>
        <Toasted message="Acknowledged." />
      </ToastProvider>,
    );
    const toast = await screen.findByRole("status");
    expect(toast).toHaveTextContent("Acknowledged.");
    await user.click(within(toast).getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByRole("status")).toBeNull();
  });
});

describe("the login screen (docs/54)", () => {
  it("puts the focus on the username, with the autocomplete password managers need", async () => {
    const { fetcher } = gateway("out");
    open(fetcher);
    const form = await screen.findByRole("region", { name: "Log in" });
    const username = within(form).getByLabelText("Username");
    await waitFor(() => {
      expect(username).toHaveFocus();
    });
    expect(username).toHaveAttribute("autocomplete", "username");
    expect(within(form).getByLabelText("Password")).toHaveAttribute(
      "autocomplete",
      "current-password",
    );
  });

  it("shows and hides the password, warns of Caps Lock, and submits with Enter", async () => {
    const { fetcher, seen } = gateway("out");
    const user = open(fetcher);
    const form = await screen.findByRole("region", { name: "Log in" });
    const password = within(form).getByLabelText("Password");
    expect(password).toHaveAttribute("type", "password");
    await user.click(within(form).getByRole("button", { name: "Show password" }));
    expect(password).toHaveAttribute("type", "text");
    expect(within(form).getByRole("button", { name: "Hide password" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await user.click(within(form).getByRole("button", { name: "Hide password" }));
    expect(password).toHaveAttribute("type", "password");
    await user.click(password);
    await user.keyboard("{CapsLock}a");
    expect(within(form).getByRole("status")).toHaveTextContent("Caps Lock is on.");
    expect(password).toHaveAccessibleDescription("Caps Lock is on.");
    await user.keyboard("{CapsLock}");
    await user.clear(password);
    await user.type(within(form).getByLabelText("Username"), "someone");
    await user.type(password, `${PASSWORD}{Enter}`);
    await screen.findByRole("navigation", { name: "Sections" });
    expect(seen.some((s) => s.method === "POST" && s.path === "/v1/auth/login")).toBe(true);
  });

  it("says the gateway's one refusal in words, with the lockout rule, and empties the password", async () => {
    const { fetcher } = gateway("out");
    const user = open(fetcher);
    const form = await screen.findByRole("region", { name: "Log in" });
    await user.type(within(form).getByLabelText("Username"), "someone");
    await user.type(within(form).getByLabelText("Password"), "not-the-password");
    await user.click(within(form).getByRole("button", { name: "Log in" }));
    const alert = await within(form).findByRole("alert");
    expect(alert).toHaveTextContent("refused: invalid_credentials");
    expect(alert).toHaveTextContent(LOCKOUT_SENTENCE);
    expect(within(form).getByLabelText("Password")).toHaveValue("");
  });
});

describe("the forced password change (docs/54)", () => {
  it("shows only its form and Log out: the rules before typing, no navigation, no account menu", async () => {
    const { fetcher } = gateway("change");
    open(fetcher);
    const card = await screen.findByRole("region", { name: "Change your password" });
    await waitFor(() => {
      expect(within(card).getByLabelText("Current password")).toHaveFocus();
    });
    const next = within(card).getByLabelText("New password");
    expect(next).toHaveAttribute("autocomplete", "new-password");
    expect(within(card).getByLabelText("Current password")).toHaveAttribute(
      "autocomplete",
      "current-password",
    );
    for (const rule of PASSWORD_RULES) expect(card).toHaveTextContent(rule);
    expect(next).toHaveAccessibleDescription(expect.stringContaining(PASSWORD_RULES[0] ?? "?"));
    expect(within(card).getByRole("button", { name: "Log out" })).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Sections" })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Account:/ })).toBeNull();
    expect(screen.queryByRole("navigation", { name: "Breadcrumb" })).toBeNull();
  });
});

describe("a figure with too few requests behind it (docs/54)", () => {
  it("is a dash with its meaning in a tooltip, in words for screen readers, and in a footnote", async () => {
    const { fetcher } = gateway("in", capabilities.alice);
    open(fetcher);
    const table = await screen.findByRole("table", { name: "Applications" });
    const blog = within(table).getByRole("row", { name: /north-blog/ });
    // The meaning is a tooltip that describes the dash (docs/55): it takes the focus, and opens on
    // focus as well as hover, where a title attribute opened on hover only.
    const dash = within(blog).getAllByRole("button", { description: TOO_FEW_EXPLAINED })[0];
    expect(dash).toHaveTextContent(`${TOO_FEW}${TOO_FEW_WORDS}`);
    expect(screen.getAllByRole("tooltip", { hidden: true })[0]).toHaveTextContent(
      TOO_FEW_EXPLAINED,
    );
    expect(screen.getByText(`${TOO_FEW} ${TOO_FEW_EXPLAINED}`)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Add application" })).toHaveClass("primary");
  });
});
