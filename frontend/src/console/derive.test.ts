import {
  canSave,
  canWrite,
  capRows,
  draftChange,
  formatTime,
  formatValue,
  initialDraft,
  limitRows,
  parseRoute,
  presetRows,
  refusalText,
  routeHash,
  settingRows,
  verdict,
  versionRows,
} from "./derive";
import type { ApplicationSettings, OrganizationSettings, Validation } from "./types";

function application(overrides: Partial<ApplicationSettings> = {}): ApplicationSettings {
  return {
    application_id: "app-one",
    organization_id: "acme",
    mode: { organization: "local_first", application: "local_first", effective: "local_first" },
    version: 2,
    settings: { max_hosted_attempts: 1 },
    effective: {
      requests_per_minute: {
        value: 600,
        source: "organization",
        capped_by: "organization",
        stored: null,
        caps: { organization: 600 },
      },
      max_hosted_attempts: {
        value: 1,
        source: "application",
        capped_by: null,
        stored: 1,
        caps: { platform: 3 },
      },
      allowed_tiers: {
        value: ["economy", "standard"],
        source: "plan",
        capped_by: null,
        stored: null,
        caps: {},
      },
    },
    ignored_aliases: [],
    ...overrides,
  };
}

const organization: OrganizationSettings = {
  organization_id: "acme",
  mode: "local_first",
  version: 1,
  settings: { requests_per_minute: 600 },
  platform_caps: { requests_per_minute: {}, max_hosted_attempts: { platform: 3 } },
};

function validation(overrides: Partial<Validation> = {}): Validation {
  return {
    valid: true,
    code: null,
    message: null,
    invalid_setting: null,
    refusals: [],
    changed: ["max_hosted_attempts"],
    settings: {},
    loosens: [],
    step_up_required: false,
    effective: { max_hosted_attempts: 1, requests_per_minute: 600 },
    ...overrides,
  };
}

describe("canWrite", () => {
  it("shows write controls only when the gateway said yes", () => {
    expect(canWrite("allowed")).toBe(true);
    expect(canWrite("forbidden")).toBe(false);
    expect(canWrite("unknown")).toBe(false);
    expect(canWrite(null)).toBe(false);
  });
});

describe("formatValue", () => {
  it("prints JSON, and nothing set as such", () => {
    expect(formatValue(600)).toBe("600");
    expect(formatValue(["economy"])).toBe('["economy"]');
    expect(formatValue("all")).toBe('"all"');
    expect(formatValue(false)).toBe("false");
    expect(formatValue(null)).toBe("not set");
    expect(formatValue(undefined)).toBe("not set");
  });
});

describe("settingRows", () => {
  it("keeps the gateway's order and says where each value came from", () => {
    const rows = settingRows(application());
    expect(rows.map((r) => r.name)).toEqual([
      "requests_per_minute",
      "max_hosted_attempts",
      "allowed_tiers",
    ]);
    expect(rows[0]).toEqual({
      name: "requests_per_minute",
      value: "600",
      source: "organization",
      cappedBy: "organization",
      stored: "not set",
      caps: "organization: 600",
    });
    expect(rows[1]?.stored).toBe("1");
    expect(rows[2]?.caps).toBe("none");
    expect(rows[2]?.cappedBy).toBe("-");
  });
});

describe("capRows and limitRows", () => {
  it("lists every cap the organization may set, beside what it set", () => {
    expect(capRows(organization)).toEqual([
      { name: "requests_per_minute", organization: "600", platform: "none" },
      { name: "max_hosted_attempts", organization: "not set", platform: "platform: 3" },
    ]);
  });

  it("names the applications the gateway says each cap holds down", () => {
    // What the gateway answers for an application that set nothing below the cap: the cap is
    // the value (source organization), and nothing was lowered (capped_by null).
    const capped = application({
      effective: {
        ...application().effective,
        requests_per_minute: {
          value: 600,
          source: "organization",
          capped_by: null,
          stored: null,
          caps: { organization: 600 },
        },
      },
    });
    const free = application({
      effective: {
        ...application().effective,
        requests_per_minute: {
          value: 100,
          source: "application",
          capped_by: null,
          stored: 100,
          caps: { organization: 600 },
        },
      },
    });
    const lowered = application({
      effective: {
        ...application().effective,
        requests_per_minute: {
          value: 600,
          source: "application",
          capped_by: "organization",
          stored: 900,
          caps: { organization: 600 },
        },
      },
    });
    const summary = (id: string) => ({
      application_id: id,
      name: `App ${id}`,
      organization_id: "acme",
      plan: "pro",
    });
    const rows = limitRows(organization, [
      { summary: summary("A"), settings: capped },
      { summary: summary("B"), settings: free },
      { summary: summary("C"), settings: lowered },
    ]);
    // A: the cap is its value; C: the cap lowered what it stored; B: below the cap.
    expect(rows).toEqual([{ name: "requests_per_minute", applications: ["App A", "App C"] }]);
  });
});

describe("versionRows", () => {
  it("says who, how and when, in UTC", () => {
    expect(
      versionRows([
        {
          version: 2,
          changed_fields: ["a", "b"],
          changed_by_user_id: "usr_1",
          via: "http",
          changed_at_ms: 0,
        },
        {
          version: 1,
          changed_fields: [],
          changed_by_user_id: null,
          via: "cli",
          changed_at_ms: 1000,
        },
      ]),
    ).toEqual([
      { version: 2, fields: "a, b", by: "usr_1", via: "http", when: "1970-01-01 00:00:00 UTC" },
      {
        version: 1,
        fields: "none",
        by: "the operator (CLI)",
        via: "cli",
        when: "1970-01-01 00:00:01 UTC",
      },
    ]);
    expect(formatTime(Date.UTC(2026, 10, 9, 4, 52, 55))).toBe("2026-11-09 04:52:55 UTC");
  });
});

describe("the edit form's draft", () => {
  it("starts with every setting kept, showing what is stored, else what is in force", () => {
    const draft = initialDraft(application());
    expect(draft.max_hosted_attempts).toEqual({ mode: "keep", text: "1" });
    expect(draft.requests_per_minute).toEqual({ mode: "keep", text: "600" });
    expect(draftChange(draft)).toEqual({ set: {}, unset: [], unparsed: [], empty: true });
  });

  it("sends what is set as parsed JSON and what is unset by name, judging nothing else", () => {
    const change = draftChange({
      a: { mode: "set", text: "5000" },
      b: { mode: "unset", text: "1" },
      c: { mode: "keep", text: "not json" },
      d: { mode: "set", text: '"all"' },
    });
    expect(change).toEqual({
      set: { a: 5000, d: "all" },
      unset: ["b"],
      unparsed: [],
      empty: false,
    });
  });

  it("names a field whose text is not JSON", () => {
    expect(draftChange({ a: { mode: "set", text: "{" } }).unparsed).toEqual(["a"]);
  });
});

describe("verdict and canSave", () => {
  const change = draftChange({ max_hosted_attempts: { mode: "set", text: "1" } });

  it("waits for the gateway's dry run before offering Save", () => {
    expect(verdict(change, null)).toEqual({ kind: "checking" });
    expect(canSave(verdict(change, null))).toBe(false);
  });

  it("offers Save for a valid change that changes something, and shows what would be in force", () => {
    const shown = verdict(change, validation());
    expect(shown).toEqual({
      kind: "valid",
      changed: ["max_hosted_attempts"],
      stepUp: false,
      effective: [["max_hosted_attempts", "1"]],
    });
    expect(canSave(shown)).toBe(true);
    expect(canSave(verdict(change, validation({ changed: [] })))).toBe(false);
  });

  it("passes on the gateway's word that saving needs the password", () => {
    const shown = verdict(
      change,
      validation({ step_up_required: true, loosens: ["max_hosted_attempts"] }),
    );
    expect(shown.kind === "valid" && shown.stepUp).toBe(true);
  });

  it("shows the gateway's refusal and does not offer Save", () => {
    const shown = verdict(
      change,
      validation({
        valid: false,
        code: "setting_looser_than_cap",
        message: "looser than the organization's cap",
        refusals: [{ setting: "requests_per_minute", level: "organization" }],
      }),
    );
    expect(shown).toEqual({
      kind: "invalid",
      message: "looser than the organization's cap",
      setting: null,
      refusals: ["requests_per_minute (looser than the organization's cap)"],
    });
    expect(canSave(shown)).toBe(false);
  });

  it("says nothing changed, or which fields are not JSON, without asking the gateway", () => {
    expect(verdict(draftChange({}), null)).toEqual({ kind: "nothing" });
    expect(verdict(draftChange({ a: { mode: "set", text: "x" } }), null)).toEqual({
      kind: "unparsed",
      fields: ["a"],
    });
  });
});

describe("presetRows", () => {
  it("lists each preset's exact values", () => {
    expect(presetRows({ cheap: { allowed_tiers: ["economy"], max_hosted_attempts: 1 } })).toEqual([
      {
        name: "cheap",
        values: [
          ["allowed_tiers", '["economy"]'],
          ["max_hosted_attempts", "1"],
        ],
      },
    ]);
  });
});

describe("routes", () => {
  it("reads and writes the two screens in the hash", () => {
    expect(parseRoute("")).toEqual({ screen: "application", applicationId: null });
    expect(parseRoute("#/organization")).toEqual({ screen: "organization" });
    expect(parseRoute("#/application/app%20one")).toEqual({
      screen: "application",
      applicationId: "app one",
    });
    expect(routeHash({ screen: "application", applicationId: "a/b" })).toBe("#/application/a%2Fb");
    expect(routeHash({ screen: "application", applicationId: null })).toBe("#/application");
    expect(routeHash({ screen: "organization" })).toBe("#/organization");
  });
});

describe("refusalText", () => {
  it("answers forbidden and missing alike, and passes the gateway's other messages on", () => {
    expect(refusalText("forbidden", "anything")).toBe("Not permitted, or not found.");
    expect(refusalText("invalid_request", "The request is not valid.")).toBe(
      "The request is not valid.",
    );
  });
});
