import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Shell } from "../shell/Shell";
import { capabilities } from "../test/fixtures";
import type { ApplicationSettings } from "./types";

const ORG = "/v1/admin/orgs/acme";
const APP = `${ORG}/applications/app-one/settings`;
const PASSWORD = "planted-password-7f3a";
const CSRF = "planted-csrf-91c2";

interface Seen {
  method: string;
  path: string;
  csrf: string | undefined;
  body: unknown;
}

interface Gateway {
  fetcher: typeof fetch;
  seen: Seen[];
}

function settings(version: number): ApplicationSettings {
  return {
    application_id: "app-one",
    organization_id: "acme",
    mode: { organization: "local_first", application: "local_first", effective: "local_first" },
    version,
    settings: {},
    effective: {
      max_hosted_attempts: {
        value: 3,
        source: "platform",
        capped_by: null,
        stored: null,
        caps: {},
      },
    },
    ignored_aliases: [],
  };
}

/**
 * A stand-in for the gateway's admin endpoints, enough for the console's flows: `probe` is
 * what the dry run of an empty change answers (who may write is the gateway's answer).
 */
function gateway(options: {
  loggedIn?: boolean;
  probe?: { status: number; code?: string };
  stepUpOnPut?: "validate-says" | "put-says";
  /** The session ends at this call: it answers 401 session_required. */
  endsAt?: "put" | "step-up";
}): Gateway {
  const seen: Seen[] = [];
  let version = 1;
  let steppedUp = false;
  const json = (status: number, body: unknown) =>
    Promise.resolve(new Response(JSON.stringify(body), { status }));
  const refuse = (status: number, code: string) =>
    json(status, { error: { code, message: `refused: ${code}` } });
  const user = {
    user_id: "usr_1",
    username: "admin-e2e",
    organization_id: "acme",
    must_change_password: false,
  };

  const fetcher = (path: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const headers = (init?.headers ?? {}) as Record<string, string>;
    const body: unknown = typeof init?.body === "string" ? JSON.parse(init.body) : undefined;
    seen.push({ method, path, csrf: headers["X-CSRF-Token"], body });
    const key = `${method} ${path.split("?")[0] ?? ""}`;
    switch (key) {
      case "GET /v1/auth/me":
        return options.loggedIn === true
          ? json(200, { user, csrf_token: CSRF, step_up_until_ms: null })
          : refuse(401, "session_required");
      case "GET /v1/auth/capabilities":
        // What the gateway answers an organization's admin (a fixture it produced).
        return json(200, capabilities.alice);
      case "POST /v1/auth/login": {
        const given = body as { password: string };
        return given.password === PASSWORD
          ? json(200, { user, csrf_token: CSRF })
          : refuse(401, "invalid_credentials");
      }
      case "POST /v1/auth/step-up": {
        if (options.endsAt === "step-up") return refuse(401, "session_required");
        const given = body as { password: string };
        if (given.password !== PASSWORD) return refuse(403, "step_up_failed");
        steppedUp = true;
        return json(200, { step_up_until_ms: 1 });
      }
      case `GET ${ORG}/applications`:
        return json(200, {
          applications: [
            { application_id: "app-one", name: "App one", organization_id: "acme", plan: "pro" },
          ],
          next: null,
        });
      case `GET ${APP}`:
        return json(200, settings(version));
      case `GET ${APP}/versions`:
        return json(200, { versions: [], next_after: null });
      case `GET ${ORG}/settings/presets`:
        return json(200, { presets: { cheap: { max_hosted_attempts: 1 } } });
      case `POST ${APP}/validate`: {
        const change = body as { set: Record<string, unknown>; unset: string[] };
        const empty = Object.keys(change.set).length === 0 && change.unset.length === 0;
        if (empty && options.probe !== undefined && options.probe.status !== 200) {
          return refuse(options.probe.status, options.probe.code ?? "forbidden");
        }
        // 99 attempts is looser than a cap: the dry run refuses it.
        if (change.set.max_hosted_attempts === 99) {
          return json(200, {
            valid: false,
            code: "settings_invalid",
            message: "Looser than the platform's cap.",
            invalid_setting: "max_hosted_attempts",
            refusals: [{ setting: "max_hosted_attempts", level: "platform" }],
            changed: [],
            settings: {},
            loosens: [],
            step_up_required: false,
          });
        }
        return json(200, {
          valid: true,
          code: null,
          message: null,
          invalid_setting: null,
          refusals: [],
          changed: empty ? [] : ["max_hosted_attempts"],
          settings: {},
          loosens: [],
          step_up_required: options.stepUpOnPut === "validate-says",
          effective: { max_hosted_attempts: 1 },
        });
      }
      case `GET ${ORG}/settings`:
        return json(200, {
          organization_id: "acme",
          mode: "local_first",
          version,
          settings: { requests_per_minute: 600 },
          platform_caps: { requests_per_minute: {}, max_hosted_attempts: { platform: 3 } },
        });
      case `GET ${ORG}/settings/versions`:
        return json(200, { versions: [], next_after: null });
      case `POST ${ORG}/settings/validate`: {
        const change = body as { set: object; unset: string[] };
        const empty = Object.keys(change.set).length === 0 && change.unset.length === 0;
        if (empty && options.probe !== undefined && options.probe.status !== 200) {
          return refuse(options.probe.status, options.probe.code ?? "forbidden");
        }
        return json(200, {
          valid: true,
          code: null,
          message: null,
          invalid_setting: null,
          refusals: [],
          changed: empty ? [] : Object.keys(change.set),
          settings: {},
          loosens: [],
          step_up_required: false,
        });
      }
      case `PUT ${ORG}/settings`:
        version += 1;
        return json(200, { version, changed: ["requests_per_minute"], settings: {} });
      case `PUT ${APP}`:
        if (options.endsAt === "put") return refuse(401, "session_required");
        if (options.stepUpOnPut !== undefined && !steppedUp) return refuse(403, "step_up_required");
        steppedUp = false;
        version += 1;
        return json(200, { version, changed: ["max_hosted_attempts"], settings: {} });
      default:
        return refuse(404, "not_found");
    }
  };
  return { fetcher: fetcher as unknown as typeof fetch, seen };
}

/** Every key and value a storage holds. */
function dump(storage: Storage): string {
  return Array.from({ length: storage.length }, (_, i) => {
    const key = storage.key(i) ?? "";
    return `${key}=${storage.getItem(key) ?? ""}`;
  }).join(String.fromCharCode(10));
}

async function openAppOne() {
  const user = userEvent.setup();
  // The page opens at home; the settings are a tab of the application's page (docs/56).
  await screen.findByRole("navigation", { name: "Sections" });
  window.location.hash = "#/app/app-one/settings";
  await screen.findByRole("region", { name: "Settings in force" });
  return user;
}

function writeControls() {
  const main = screen.getByRole("main");
  return [
    ...within(main).queryAllByRole("button", { name: /^(Save|Apply |Revert)/ }),
    ...within(main).queryAllByRole("region", { name: "Edit" }),
  ];
}

describe("the settings screens, in the page", () => {
  it("logs in, keeps the CSRF token out of storage, and sends it on every change", async () => {
    const { fetcher, seen } = gateway({ probe: { status: 200 } });
    render(<Shell fetcher={fetcher} />);
    const user = userEvent.setup();
    const form = await screen.findByRole("region", { name: "Log in" });
    await user.type(within(form).getByLabelText("Username"), "admin-e2e");
    await user.type(within(form).getByLabelText("Password"), PASSWORD);
    await user.click(within(form).getByRole("button", { name: "Log in" }));
    // Logged in: the account menu is there (docs/54).
    await screen.findByRole("button", { name: /^Account:/ });
    await openAppOne();
    await screen.findByText("May change");

    const changes = seen.filter((s) => s.method !== "GET" && s.path !== "/v1/auth/login");
    expect(changes.length).toBeGreaterThan(0);
    for (const call of changes) expect(call.csrf).toBe(CSRF);
    expect(seen.filter((s) => s.method === "GET").every((s) => s.csrf === undefined)).toBe(true);
    const stored = dump(localStorage) + dump(sessionStorage);
    expect(stored).not.toContain(CSRF);
    expect(stored).not.toContain(PASSWORD);
    expect(document.body.innerHTML).not.toContain(PASSWORD);
    expect(document.body.innerHTML).not.toContain(CSRF);
  });

  it("asks /v1/auth/me for the session and its CSRF token on a reload, with no login", async () => {
    const { fetcher, seen } = gateway({ loggedIn: true, probe: { status: 200 } });
    render(<Shell fetcher={fetcher} />);
    // Logged in: the account menu is there (docs/54).
    await screen.findByRole("button", { name: /^Account:/ });
    await openAppOne();
    await screen.findByText("May change");
    expect(seen.find((s) => s.path.endsWith("/validate"))?.csrf).toBe(CSRF);
  });

  it("shows no write control when the gateway refused the dry run", async () => {
    const { fetcher } = gateway({ loggedIn: true, probe: { status: 403, code: "forbidden" } });
    render(<Shell fetcher={fetcher} />);
    await openAppOne();
    await screen.findByText("Read only");
    expect(screen.getByRole("region", { name: "Presets" })).toHaveTextContent("cheap");
    expect(writeControls()).toEqual([]);
    expect(within(screen.getByRole("main")).queryAllByRole("textbox")).toEqual([]);
  });

  it.each([
    [503, "state_unavailable"],
    [403, "csrf_failed"],
  ])("shows no write control when the dry run answers %i %s", async (status, code) => {
    const { fetcher } = gateway({ loggedIn: true, probe: { status, code } });
    render(<Shell fetcher={fetcher} />);
    await openAppOne();
    await screen.findByText("Read only");
    expect(writeControls()).toEqual([]);
  });

  it("logs in again where it was when the dry run answers 401 session_required", async () => {
    window.location.hash = "#/application/app-one";
    const { fetcher } = gateway({
      loggedIn: true,
      probe: { status: 401, code: "session_required" },
    });
    render(<Shell fetcher={fetcher} />);
    const login = await screen.findByRole("region", { name: "Log in" });
    expect(login).toHaveTextContent("Your session has ended. Log in again.");
    expect(window.location.hash).toBe("#/application/app-one");
    expect(writeControls()).toEqual([]);
  });

  it("asks for the password before saving a change the dry run says loosens, and keeps nothing", async () => {
    const { fetcher, seen } = gateway({
      loggedIn: true,
      probe: { status: 200 },
      stepUpOnPut: "validate-says",
    });
    render(<Shell fetcher={fetcher} />);
    const user = await openAppOne();
    const edit = await screen.findByRole("region", { name: "Edit" });
    await user.selectOptions(within(edit).getByLabelText("max_hosted_attempts: change"), "unset");
    await screen.findByText(/saving asks for your password/);
    await user.click(screen.getByRole("button", { name: "Save" }));

    const dialog = await screen.findByRole("dialog", { name: "Confirm with your password" });
    // Asked before the first try: nothing was sent yet.
    expect(seen.some((s) => s.method === "PUT")).toBe(false);
    await user.type(within(dialog).getByLabelText("Password"), PASSWORD);
    await user.click(within(dialog).getByRole("button", { name: "Confirm" }));
    await screen.findByText("Saved as version 2.");

    expect(screen.queryByRole("dialog")).toBeNull();
    const stepUp = seen.find((s) => s.path === "/v1/auth/step-up");
    expect(stepUp?.body).toEqual({ password: PASSWORD });
    expect(stepUp?.csrf).toBe(CSRF);
    expect(document.body.innerHTML).not.toContain(PASSWORD);
    for (const input of document.querySelectorAll("input")) expect(input.value).not.toBe(PASSWORD);
    const stored = dump(localStorage) + dump(sessionStorage);
    expect(stored).not.toContain(PASSWORD);
  });

  it("asks for the password when the gateway answers step_up_required, then tries once more", async () => {
    const { fetcher, seen } = gateway({
      loggedIn: true,
      probe: { status: 200 },
      stepUpOnPut: "put-says",
    });
    render(<Shell fetcher={fetcher} />);
    const user = await openAppOne();
    const edit = await screen.findByRole("region", { name: "Edit" });
    await user.selectOptions(within(edit).getByLabelText("max_hosted_attempts: change"), "set");
    await user.clear(within(edit).getByLabelText("max_hosted_attempts: value"));
    await user.type(within(edit).getByLabelText("max_hosted_attempts: value"), "1");
    await screen.findByText(/^Valid\. Changes/);
    await user.click(screen.getByRole("button", { name: "Save" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Password"), PASSWORD);
    await user.click(within(dialog).getByRole("button", { name: "Confirm" }));
    await screen.findByText("Saved as version 2.");
    expect(seen.filter((s) => s.method === "PUT")).toHaveLength(2);
  });

  it("logs in again where it was when a save answers 401 session_required", async () => {
    const { fetcher, seen } = gateway({ loggedIn: true, probe: { status: 200 }, endsAt: "put" });
    render(<Shell fetcher={fetcher} />);
    const user = await openAppOne();
    const edit = await screen.findByRole("region", { name: "Edit" });
    await user.selectOptions(within(edit).getByLabelText("max_hosted_attempts: change"), "unset");
    await screen.findByText(/^Valid\. Changes/);
    await user.click(screen.getByRole("button", { name: "Save" }));
    const login = await screen.findByRole("region", { name: "Log in" });
    expect(login).toHaveTextContent("Your session has ended. Log in again.");
    expect(window.location.hash).toBe("#/app/app-one/settings");
    // The token is forgotten: the next login carries none.
    await user.type(within(login).getByLabelText("Username"), "admin-e2e");
    await user.type(within(login).getByLabelText("Password"), PASSWORD);
    await user.click(within(login).getByRole("button", { name: "Log in" }));
    // Logged in: the account menu is there (docs/54).
    await screen.findByRole("button", { name: /^Account:/ });
    expect(seen.filter((s) => s.path === "/v1/auth/login").at(-1)?.csrf).toBeUndefined();
  });

  it("closes the password prompt and logs in again when the session ends while it is open", async () => {
    const { fetcher, seen } = gateway({
      loggedIn: true,
      probe: { status: 200 },
      stepUpOnPut: "validate-says",
      endsAt: "step-up",
    });
    render(<Shell fetcher={fetcher} />);
    const user = await openAppOne();
    const edit = await screen.findByRole("region", { name: "Edit" });
    await user.selectOptions(within(edit).getByLabelText("max_hosted_attempts: change"), "unset");
    await screen.findByText(/saving asks for your password/);
    await user.click(screen.getByRole("button", { name: "Save" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Password"), PASSWORD);
    await user.click(within(dialog).getByRole("button", { name: "Confirm" }));
    const login = await screen.findByRole("region", { name: "Log in" });
    expect(login).toHaveTextContent("Your session has ended. Log in again.");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(seen.some((s) => s.method === "PUT")).toBe(false);
    for (const input of document.querySelectorAll("input")) expect(input.value).not.toBe(PASSWORD);
  });

  it("changes nothing when the password is not given", async () => {
    const { fetcher, seen } = gateway({
      loggedIn: true,
      probe: { status: 200 },
      stepUpOnPut: "validate-says",
    });
    render(<Shell fetcher={fetcher} />);
    const user = await openAppOne();
    const edit = await screen.findByRole("region", { name: "Edit" });
    await user.selectOptions(within(edit).getByLabelText("max_hosted_attempts: change"), "unset");
    await screen.findByText(/saving asks for your password/);
    await user.click(screen.getByRole("button", { name: "Save" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await screen.findByText("Not changed: the password was not given.");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(seen.some((s) => s.method === "PUT")).toBe(false);
  });
});

describe("the typed settings form (docs/55)", () => {
  it("sets a number in its field, lists the change with its old and new value, and saves it", async () => {
    const { fetcher, seen } = gateway({ loggedIn: true, probe: { status: 200 } });
    render(<Shell fetcher={fetcher} />);
    const user = await openAppOne();
    const edit = await screen.findByRole("region", { name: "Edit" });
    const field = within(edit).getByRole("spinbutton", { name: "max_hosted_attempts" });
    expect(field).toHaveAccessibleDescription("attempts");
    expect(field).toHaveValue(3);
    await user.clear(field);
    await user.type(field, "1");
    const changes = within(edit).getByRole("region", { name: "Changes" });
    expect(changes).toHaveTextContent("max_hosted_attempts: 3 attempts \u2192 1 attempts");
    await screen.findByText(/^Valid\. Changes/);
    const sent = seen.filter((s) => s.method === "POST" && s.path === `${APP}/validate`).at(-1);
    expect(sent?.body).toEqual({ expected_version: 1, set: { max_hosted_attempts: 1 }, unset: [] });
    const bar = within(edit).getByRole("region", { name: "Save changes" });
    expect(bar).toHaveTextContent("1 change");
    await user.click(within(bar).getByRole("button", { name: "Save" }));
    await screen.findByText("Saved as version 2.");
    expect(seen.find((s) => s.method === "PUT")?.body).toEqual({
      expected_version: 1,
      set: { max_hosted_attempts: 1 },
      unset: [],
    });
  });

  it("offers Save only once the gateway's dry run says the change is valid", async () => {
    const { fetcher, seen } = gateway({ loggedIn: true, probe: { status: 200 } });
    render(<Shell fetcher={fetcher} />);
    const user = await openAppOne();
    const edit = await screen.findByRole("region", { name: "Edit" });
    const field = within(edit).getByRole("spinbutton", { name: "max_hosted_attempts" });
    await user.clear(field);
    await user.type(field, "99");
    await screen.findByText(/^Refused \(max_hosted_attempts\)/);
    const save = within(edit).getByRole("button", { name: "Save" });
    expect(save).toBeDisabled();
    await user.click(save);
    expect(seen.some((s) => s.method === "PUT")).toBe(false);
    await user.clear(field);
    await user.type(field, "1");
    await screen.findByText(/^Valid\. Changes/);
    expect(within(edit).getByRole("button", { name: "Save" })).toBeEnabled();
  });

  it("discards every change from the Save bar, and the bar goes", async () => {
    const { fetcher } = gateway({ loggedIn: true, probe: { status: 200 } });
    render(<Shell fetcher={fetcher} />);
    const user = await openAppOne();
    const edit = await screen.findByRole("region", { name: "Edit" });
    const field = within(edit).getByRole("spinbutton", { name: "max_hosted_attempts" });
    await user.clear(field);
    await user.type(field, "2");
    await user.click(
      within(within(edit).getByRole("region", { name: "Save changes" })).getByRole("button", {
        name: "Discard",
      }),
    );
    expect(field).toHaveValue(3);
    expect(within(edit).queryByRole("region", { name: "Save changes" })).toBeNull();
    expect(within(edit).getByRole("region", { name: "Changes" })).toHaveTextContent(
      "Nothing changed.",
    );
  });

  it("keeps the JSON editing under Advanced, on the same draft", async () => {
    const { fetcher } = gateway({ loggedIn: true, probe: { status: 200 } });
    render(<Shell fetcher={fetcher} />);
    const user = await openAppOne();
    const edit = await screen.findByRole("region", { name: "Edit" });
    await user.click(within(edit).getByText("Advanced: edit as JSON"));
    await user.selectOptions(within(edit).getByLabelText("max_hosted_attempts: change"), "set");
    await user.clear(within(edit).getByLabelText("max_hosted_attempts: value"));
    await user.type(within(edit).getByLabelText("max_hosted_attempts: value"), "2");
    expect(within(edit).getByRole("spinbutton", { name: "max_hosted_attempts" })).toHaveValue(2);
  });
});

describe("the organization's settings form (docs/52 section 16.7)", () => {
  async function openOrganization() {
    const user = userEvent.setup();
    const sections = await screen.findByRole("navigation", { name: "Sections" });
    await user.click(within(sections).getByRole("link", { name: "Organization settings" }));
    await screen.findByRole("region", { name: "Caps" });
    return user;
  }

  it("shows the form when the gateway's dry run allows it, and saves through the organization's endpoint", async () => {
    const { fetcher, seen } = gateway({ loggedIn: true, probe: { status: 200 } });
    render(<Shell fetcher={fetcher} />);
    const user = await openOrganization();
    await screen.findByText("May change");
    const edit = screen.getByRole("region", { name: "Edit" });
    const field = within(edit).getByRole("spinbutton", { name: "requests_per_minute" });
    await user.clear(field);
    await user.type(field, "300");
    await screen.findByText(/^Valid\. Changes/);
    await user.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByText("Saved as version 2.");
    const put = seen.find((s) => s.method === "PUT");
    expect(put?.path).toBe(`${ORG}/settings`);
    expect(put?.body).toEqual({
      expected_version: 1,
      set: { requests_per_minute: 300 },
      unset: [],
    });
    expect(put?.csrf).toBe(CSRF);
  });

  it.each([
    [403, "forbidden"],
    [503, "state_unavailable"],
  ])("shows no form and no Revert when the dry run answers %i %s", async (status, code) => {
    const { fetcher } = gateway({ loggedIn: true, probe: { status, code } });
    render(<Shell fetcher={fetcher} />);
    await openOrganization();
    await screen.findByText("Read only");
    expect(writeControls()).toEqual([]);
    expect(within(screen.getByRole("main")).queryAllByRole("spinbutton")).toEqual([]);
  });
});
