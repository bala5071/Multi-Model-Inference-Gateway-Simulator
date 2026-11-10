import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConsoleApp } from "./ConsoleApp";
import type { AlertRule, Channel, Incident } from "./types";

const ORG = "/v1/admin/orgs/acme";
const APP = `${ORG}/applications/app-one`;
const CSRF = "planted-csrf-41d0";
const PASSWORD = "planted-password-3b9e";
const HOOK_TOKEN = "planted-hook-token-7e21";
const HOOK_URL = `https://hooks.example.com/services/${HOOK_TOKEN}`;
const SIGNING_KEY = "planted-signing-key-c0ffee-0123456789";
const CODE = "PLANT-XDZ23";
const ADDRESS = "planted-oncall@example.com";

interface Seen {
  method: string;
  path: string;
  csrf: string | undefined;
  body: string | undefined;
}

type Answer = { status: number; body: unknown };
const ok = (body: unknown): Answer => ({ status: 200, body });
const refuse = (status: number, code: string): Answer => ({
  status,
  body: { error: { code, message: `refused: ${code}` } },
});

const RULE: AlertRule = {
  id: "rul_1",
  version: 1,
  default_key: "key_expires",
  changed_from_default: false,
  name: "Key expires within 14 days",
  signal: "key_expires_in",
  parameter: null,
  comparison: "<=",
  threshold: 14,
  window: null,
  for_minutes: 0,
  severity: "warning",
  channels: ["organization defaults"],
  cooldown_minutes: 30,
  repeat_every_minutes: null,
  quiet_hours: null,
  enabled: true,
  created_at_ms: 0,
  updated_at_ms: 0,
};

const INCIDENT: Incident = {
  id: "inc_1",
  application_id: "app-one",
  rule_id: "rul_1",
  rule_version: 1,
  series: "key_1",
  state: "firing",
  severity: "warning",
  value: 2.5,
  opened_at_ms: 60_000,
  pending_since_ms: 60_000,
  fired_at_ms: 60_000,
  acknowledged_at_ms: null,
  acknowledged_by: null,
  resolved_at_ms: null,
  resolve_reason: null,
  no_data_since_ms: null,
  reopened: false,
};

function emailChannel(state: string): Channel {
  return {
    id: "chn_mail",
    name: "On call",
    type: "email",
    enabled: true,
    default: true,
    url_origin: null,
    version: 1,
    created_at_ms: 0,
    updated_at_ms: 0,
    health: { last_delivered_at_ms: null, last_failed_at_ms: null, consecutive_failures: 0 },
    recipients: [
      {
        id: "rcp_1",
        address: ADDRESS,
        state,
        added_at_ms: 0,
        verified_at_ms: null,
        confirmation_expires_at_ms: 86_400_000,
      },
    ],
  };
}

/**
 * A stand-in for the gateway's alert endpoints. `probe` is what a body the route refuses gets
 * (who may write is the gateway's answer); `channels` what the channel list answers.
 */
function gateway(
  options: {
    health?: Answer | "fail";
    probe?: Answer;
    confirmProbe?: Answer;
    channels?: Answer;
    incident?: Incident;
    /** Held until it settles: the confirm's answer, as a slow gateway gives it. */
    confirmGate?: Promise<void>;
  } = {},
) {
  const seen: Seen[] = [];
  let steppedUp = false;
  let recipientState = "unverified";
  const user = {
    user_id: "usr_1",
    username: "admin-e2e",
    organization_id: "acme",
    must_change_password: false,
  };
  const answer = (path: string, method: string, body: unknown): Answer | "fail" => {
    const key = `${method} ${path.split("?")[0] ?? ""}`;
    const isArray = Array.isArray(body);
    switch (key) {
      case "GET /v1/auth/me":
        return ok({ user, csrf_token: CSRF, step_up_until_ms: null });
      case "POST /v1/auth/step-up":
        if ((body as { password: string }).password !== PASSWORD) {
          return refuse(403, "step_up_failed");
        }
        steppedUp = true;
        return ok({ step_up_until_ms: 1 });
      case "GET /healthz":
        return options.health ?? ok({ status: "ok", alert_evaluator: "ok" });
      case `GET ${ORG}/applications`:
        return ok({
          applications: [
            { application_id: "app-one", name: "App one", organization_id: "acme", plan: "pro" },
          ],
          next: null,
        });
      case `GET ${APP}/alert-rules`:
        return ok({ rules: [RULE], next: null });
      case `POST ${APP}/alert-rules`:
        if (isArray) return options.probe ?? refuse(422, "invalid_request");
        return ok(RULE);
      case `POST ${APP}/alert-rules/validate`:
        return ok({ valid: true, invalid_field: null, message: null, rule: body, preview: null });
      case `GET ${APP}/incidents`:
      case `GET ${ORG}/incidents`:
        return ok({ incidents: [options.incident ?? INCIDENT], next: null });
      case `GET ${APP}/incidents/inc_1`:
        return ok({ ...(options.incident ?? INCIDENT), notifications: [] });
      case `POST ${APP}/incidents/inc_1/acknowledge`:
        return ok({ ...INCIDENT, state: "acknowledged" });
      case `GET ${APP}/alert-channels`:
        return ok({ channels: [] });
      case `GET ${ORG}/alert-channels`:
        return options.channels ?? ok({ channels: [emailChannel(recipientState)], next: null });
      case `POST ${ORG}/alert-channels`: {
        if (!steppedUp) return refuse(403, "step_up_required");
        steppedUp = false;
        const made = body as { name: string };
        return {
          status: 201,
          body: {
            ...emailChannel("unverified"),
            id: "chn_hook",
            type: "webhook",
            name: made.name,
            url_origin: "https://hooks.example.com",
            recipients: undefined,
            signing_key: SIGNING_KEY,
          },
        };
      }
      case `GET ${ORG}/alert-channels/chn_mail`:
        return ok(emailChannel(recipientState));
      case `GET ${ORG}/alert-channels/chn_mail/deliveries`:
        return ok({ deliveries: [], next: null });
      case `POST ${ORG}/alert-channels/chn_mail/recipients/rcp_probe/confirm`:
        return options.confirmProbe ?? refuse(422, "invalid_request");
      case `POST ${ORG}/alert-channels/chn_mail/recipients/rcp_1/confirm`:
        recipientState = "verified";
        return ok({ ...emailChannel("verified").recipients?.[0], state: "verified" });
      default:
        return refuse(404, "not_found");
    }
  };
  const fetcher = (path: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const headers = (init?.headers ?? {}) as Record<string, string>;
    const raw = typeof init?.body === "string" ? init.body : undefined;
    seen.push({ method, path, csrf: headers["X-CSRF-Token"], body: raw });
    const found = answer(path, method, raw === undefined ? undefined : JSON.parse(raw));
    if (found === "fail") return Promise.reject(new TypeError("network"));
    const response = new Response(JSON.stringify(found.body), { status: found.status });
    if (options.confirmGate !== undefined && path.endsWith("/recipients/rcp_1/confirm")) {
      return options.confirmGate.then(() => response);
    }
    return Promise.resolve(response);
  };
  return { fetcher: fetcher as unknown as typeof fetch, seen };
}

function dump(storage: Storage): string {
  return Array.from({ length: storage.length }, (_, i) => {
    const key = storage.key(i) ?? "";
    return `${key}=${storage.getItem(key) ?? ""}`;
  }).join(String.fromCharCode(10));
}

function kept(): string {
  return dump(localStorage) + dump(sessionStorage) + document.cookie + window.location.href;
}

function writeControls() {
  const main = screen.getByRole("main");
  return [
    ...within(main).queryAllByRole("button", {
      name: /^(Edit |Copy |Disable|Enable|Delete|Restore|Save rule|Create channel|Test send|Remove |Send the code|Confirm|Add|Acknowledge)/,
    }),
    ...within(main).queryAllByRole("textbox"),
  ];
}

function go(hash: string) {
  window.location.hash = hash;
}

afterEach(() => {
  window.location.hash = "";
});

describe("the evaluator banner", () => {
  it.each([
    ["#/rules/app-one", "stale"],
    ["#/channels", "unknown"],
    ["#/incidents", "stale"],
  ])("shows on %s while /healthz says %s", async (hash, state) => {
    go(hash);
    const { fetcher } = gateway({ health: ok({ status: "ok", alert_evaluator: state }) });
    render(<ConsoleApp fetcher={fetcher} />);
    expect(await screen.findByRole("alert", { name: "Alert evaluation" })).toHaveTextContent(
      `Alert evaluation: ${state}.`,
    );
  });

  it("shows unreadable when /healthz cannot be read", async () => {
    go("#/incidents");
    const { fetcher } = gateway({ health: "fail" });
    render(<ConsoleApp fetcher={fetcher} />);
    expect(await screen.findByRole("alert", { name: "Alert evaluation" })).toHaveTextContent(
      "unreadable",
    );
  });

  it("shows nothing when the evaluator is ok", async () => {
    go("#/rules/app-one");
    const { fetcher, seen } = gateway();
    render(<ConsoleApp fetcher={fetcher} />);
    await screen.findByRole("region", { name: "Rules" });
    await waitFor(() => {
      expect(seen.some((s) => s.path === "/healthz")).toBe(true);
    });
    expect(screen.queryByRole("alert", { name: "Alert evaluation" })).toBeNull();
  });
});

describe("alert rules", () => {
  it("shows a writer the controls, on the gateway's 422 to the probe", async () => {
    go("#/rules/app-one");
    const { fetcher, seen } = gateway();
    render(<ConsoleApp fetcher={fetcher} />);
    await screen.findByText("May change");
    expect(screen.getByRole("button", { name: "Edit Key expires within 14 days" })).toBeVisible();
    const probe = seen.find((s) => s.method === "POST" && s.path === `${APP}/alert-rules`);
    expect(probe?.body).toBe("[]");
    expect(probe?.csrf).toBe(CSRF);
  });

  it.each([
    [403, "forbidden"],
    [503, "state_unavailable"],
    [401, "session_required"],
    [403, "csrf_failed"],
    [200, "accepted the array"],
  ])("shows no write control when the probe answers %i %s", async (status, code) => {
    go("#/rules/app-one");
    const probe = status === 200 ? ok(RULE) : refuse(status, code);
    const { fetcher } = gateway({ probe });
    render(<ConsoleApp fetcher={fetcher} />);
    await screen.findByText("Read only");
    expect(screen.getByRole("region", { name: "Rules" })).toHaveTextContent(
      "Key expires within 14 days",
    );
    expect(writeControls()).toEqual([]);
  });
});

describe("incidents", () => {
  it("offers Acknowledge on a firing incident when the rule probe says yes", async () => {
    go("#/incidents/app-one/inc_1");
    const { fetcher, seen } = gateway();
    render(<ConsoleApp fetcher={fetcher} />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Acknowledge" }));
    await screen.findByText("Acknowledged.");
    expect(seen.some((s) => s.path === `${APP}/incidents/inc_1/acknowledge`)).toBe(true);
  });

  it("offers no Acknowledge to an account the rule probe refuses", async () => {
    go("#/incidents/app-one/inc_1");
    const { fetcher } = gateway({ probe: refuse(403, "forbidden") });
    render(<ConsoleApp fetcher={fetcher} />);
    await screen.findByRole("list", { name: "Timeline" });
    expect(writeControls()).toEqual([]);
  });

  it("offers no Acknowledge on an incident that is not firing", async () => {
    go("#/incidents/app-one/inc_1");
    const { fetcher } = gateway({ incident: { ...INCIDENT, state: "acknowledged" } });
    render(<ConsoleApp fetcher={fetcher} />);
    await screen.findByRole("list", { name: "Timeline" });
    expect(screen.queryByRole("button", { name: "Acknowledge" })).toBeNull();
  });
});

describe("channels", () => {
  it("shows nothing of channels, and no control, when the gateway refuses the list", async () => {
    go("#/channels");
    const { fetcher } = gateway({ channels: refuse(403, "forbidden") });
    render(<ConsoleApp fetcher={fetcher} />);
    expect(
      await within(await screen.findByRole("main")).findByText("Not permitted, or not found."),
    ).toBeVisible();
    expect(writeControls()).toEqual([]);
    expect(document.body.innerHTML).not.toContain(ADDRESS);
  });

  it("never renders a webhook's URL, and shows its key once, then forgets it", async () => {
    go("#/channels");
    const { fetcher, seen } = gateway({ channels: ok({ channels: [], next: null }) });
    render(<ConsoleApp fetcher={fetcher} />);
    const user = userEvent.setup();
    const form = await screen.findByRole("region", { name: "New channel" });
    await user.type(within(form).getByLabelText("Name"), "Ops hook");
    await user.type(within(form).getByLabelText("Webhook URL"), HOOK_URL);
    await user.click(within(form).getByRole("button", { name: "Create channel" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Password"), PASSWORD);
    await user.click(within(dialog).getByRole("button", { name: "Confirm" }));

    const key = await screen.findByRole("region", { name: "Signing key" });
    expect(key).toHaveTextContent(SIGNING_KEY);
    await screen.findByText("Created Ops hook, sending to https://hooks.example.com.");
    // The control: the URL was sent, once the password was given.
    const creates = seen.filter((s) => s.method === "POST" && s.path === `${ORG}/alert-channels`);
    expect(creates.at(-1)?.body).toContain(HOOK_TOKEN);
    // Nowhere on the page, in a field, or kept by the browser.
    expect(document.body.innerHTML).not.toContain(HOOK_TOKEN);
    for (const field of document.querySelectorAll("input")) {
      expect(field.value).not.toContain(HOOK_TOKEN);
    }
    expect(kept()).not.toContain(HOOK_TOKEN);
    expect(kept()).not.toContain(SIGNING_KEY);

    // The copy button hands the key to the clipboard, and nothing else keeps it.
    const writeText = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    await user.click(within(key).getByRole("button", { name: "Copy the key" }));
    expect(writeText).toHaveBeenCalledWith(SIGNING_KEY);
    await user.click(within(key).getByRole("button", { name: "I have stored it" }));
    expect(screen.queryByRole("region", { name: "Signing key" })).toBeNull();
    expect(document.body.innerHTML).not.toContain(SIGNING_KEY);
  });

  it("verifies a recipient with the code typed, empties the field and keeps the code nowhere", async () => {
    go("#/channels");
    const { fetcher, seen } = gateway();
    render(<ConsoleApp fetcher={fetcher} />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Open On call" }));
    const field = await screen.findByLabelText(`Code for ${ADDRESS}`);
    await user.type(field, CODE);
    await user.click(screen.getByRole("button", { name: "Confirm" }));
    await screen.findByText(`${ADDRESS} is verified.`);
    // The control: the code was sent to the gateway.
    const confirm = seen.find((s) => s.path.endsWith("/recipients/rcp_1/confirm"));
    expect(confirm?.body).toBe(JSON.stringify({ code: CODE }));
    expect(confirm?.csrf).toBe(CSRF);
    // And kept nowhere: not in a field, the page or the browser's storage.
    for (const input of document.querySelectorAll("input")) expect(input.value).not.toBe(CODE);
    expect(document.body.innerHTML).not.toContain(CODE);
    expect(kept()).not.toContain(CODE);
  });

  it("empties the code's field as soon as it is sent, before the gateway answers", async () => {
    go("#/channels");
    let release: () => void = () => undefined;
    const confirmGate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const { fetcher, seen } = gateway({ confirmGate });
    render(<ConsoleApp fetcher={fetcher} />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Open On call" }));
    const field = await screen.findByLabelText(`Code for ${ADDRESS}`);
    await user.type(field, CODE);
    await user.click(screen.getByRole("button", { name: "Confirm" }));
    // Sent, the answer not yet come: the field is already empty.
    await waitFor(() => {
      expect(seen.some((s) => s.path.endsWith("/recipients/rcp_1/confirm"))).toBe(true);
    });
    expect(screen.getByLabelText(`Code for ${ADDRESS}`)).toHaveValue("");
    expect(document.body.innerHTML).not.toContain(CODE);
    release();
    await screen.findByText(`${ADDRESS} is verified.`);
  });

  it("shows no code entry when the confirm probe is refused", async () => {
    go("#/channels");
    const { fetcher } = gateway({ confirmProbe: refuse(403, "forbidden") });
    render(<ConsoleApp fetcher={fetcher} />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Open On call" }));
    await screen.findByRole("table", { name: "Recipients" });
    expect(screen.queryByLabelText(`Code for ${ADDRESS}`)).toBeNull();
    expect(screen.queryByRole("button", { name: /^Send the code again/ })).toBeNull();
  });
});
