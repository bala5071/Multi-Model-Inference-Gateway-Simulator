import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConsoleApp } from "./ConsoleApp";
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
      case "POST /v1/auth/login": {
        const given = body as { password: string };
        return given.password === PASSWORD
          ? json(200, { user, csrf_token: CSRF })
          : refuse(401, "invalid_credentials");
      }
      case "POST /v1/auth/step-up": {
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
          changed: empty ? [] : ["max_hosted_attempts"],
          settings: {},
          loosens: [],
          step_up_required: options.stepUpOnPut === "validate-says",
          effective: { max_hosted_attempts: 1 },
        });
      }
      case `PUT ${APP}`:
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
  await user.selectOptions(await screen.findByLabelText("Application"), "app-one");
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

describe("ConsoleApp", () => {
  it("logs in, keeps the CSRF token out of storage, and sends it on every change", async () => {
    const { fetcher, seen } = gateway({ probe: { status: 200 } });
    render(<ConsoleApp fetcher={fetcher} />);
    const user = userEvent.setup();
    const form = await screen.findByRole("region", { name: "Log in" });
    await user.type(within(form).getByLabelText("Username"), "admin-e2e");
    await user.type(within(form).getByLabelText("Password"), PASSWORD);
    await user.click(within(form).getByRole("button", { name: "Log in" }));
    await screen.findByRole("button", { name: "Log out" });
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
    render(<ConsoleApp fetcher={fetcher} />);
    await screen.findByRole("button", { name: "Log out" });
    await openAppOne();
    await screen.findByText("May change");
    expect(seen.find((s) => s.path.endsWith("/validate"))?.csrf).toBe(CSRF);
  });

  it("shows a viewer no write control: the gateway refused the dry run", async () => {
    const { fetcher } = gateway({ loggedIn: true, probe: { status: 403, code: "forbidden" } });
    render(<ConsoleApp fetcher={fetcher} />);
    await openAppOne();
    await screen.findByText("Read only");
    expect(screen.getByRole("region", { name: "Presets" })).toHaveTextContent("cheap");
    expect(writeControls()).toEqual([]);
    expect(within(screen.getByRole("main")).queryAllByRole("textbox")).toEqual([]);
  });

  it.each([
    [503, "state_unavailable"],
    [401, "session_required"],
    [403, "csrf_failed"],
  ])("shows no write control when the dry run answers %i %s", async (status, code) => {
    const { fetcher } = gateway({ loggedIn: true, probe: { status, code } });
    render(<ConsoleApp fetcher={fetcher} />);
    await openAppOne();
    await screen.findByText("Read only");
    expect(writeControls()).toEqual([]);
  });

  it("asks for the password before saving a change the dry run says loosens, and keeps nothing", async () => {
    const { fetcher, seen } = gateway({
      loggedIn: true,
      probe: { status: 200 },
      stepUpOnPut: "validate-says",
    });
    render(<ConsoleApp fetcher={fetcher} />);
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
    render(<ConsoleApp fetcher={fetcher} />);
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

  it("changes nothing when the password is not given", async () => {
    const { fetcher, seen } = gateway({
      loggedIn: true,
      probe: { status: 200 },
      stepUpOnPut: "validate-says",
    });
    render(<ConsoleApp fetcher={fetcher} />);
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
