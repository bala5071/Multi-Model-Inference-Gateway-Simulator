import type { Draft } from "./derive";
import { kindOf, SETTING_KINDS, unitOf } from "./settingKinds";
import {
  ARROW,
  changedRows,
  displayValue,
  fieldValue,
  organizationDraft,
  withText,
  withUndo,
  withUnset,
  withValue,
} from "./settingsForm";

const initial: Draft = {
  allow_downgrade_default: { mode: "keep", text: "true" },
  requests_per_minute: { mode: "keep", text: "600" },
  allowed_tiers: { mode: "keep", text: '["economy"]' },
};

describe("the typed settings form's draft (docs/55)", () => {
  it("sets a field when its value changes and keeps it when it is back to what was read", () => {
    const set = withValue(initial, initial, "allow_downgrade_default", false);
    expect(set.allow_downgrade_default).toEqual({ mode: "set", text: "false" });
    expect(
      withValue(set, initial, "allow_downgrade_default", true).allow_downgrade_default,
    ).toEqual({
      mode: "keep",
      text: "true",
    });
    expect(withText(initial, initial, "requests_per_minute", "120").requests_per_minute).toEqual({
      mode: "set",
      text: "120",
    });
  });

  it("unsets, and undoes back to the field as read", () => {
    const unset = withUnset(initial, "requests_per_minute");
    expect(unset.requests_per_minute?.mode).toBe("unset");
    expect(withUndo(unset, initial, "requests_per_minute")).toEqual(initial);
  });

  it("lists each change with its value before and after, in words", () => {
    let draft = withValue(initial, initial, "allowed_tiers", ["economy", "standard"]);
    draft = withUnset(draft, "requests_per_minute");
    draft = withText(draft, initial, "allow_downgrade_default", "not json");
    expect(changedRows(draft, initial)).toEqual([
      { name: "allow_downgrade_default", before: "On", after: "not json" },
      {
        name: "requests_per_minute",
        before: "600 requests a minute",
        after: "the inherited value",
      },
      { name: "allowed_tiers", before: "economy", after: "economy, standard" },
    ]);
    expect(changedRows(initial, initial)).toEqual([]);
  });

  it("writes values in words", () => {
    expect(displayValue("retry_invalid_tool_json", false)).toBe("Off");
    expect(displayValue("hosted_first_token_ms", 3000)).toBe("3000 ms");
    expect(displayValue("max_tokens", null)).toBe("not set");
    expect(displayValue("allowed_providers", "all")).toBe("All providers");
    expect(displayValue("allowed_providers", [])).toBe("none");
    expect(displayValue("model_aliases", { fast: "economy" })).toBe(`fast ${ARROW} "economy"`);
    expect(fieldValue({ mode: "set", text: "{" })).toBeUndefined();
  });

  it("drafts an organization's caps: every one it may set, null where it set none", () => {
    const draft = organizationDraft({
      organization_id: "acme",
      mode: "local_only",
      version: 2,
      settings: { requests_per_minute: 600 },
      platform_caps: { requests_per_minute: {}, max_hosted_attempts: { platform: 3 } },
    });
    expect(draft).toEqual({
      requests_per_minute: { mode: "keep", text: "600" },
      max_hosted_attempts: { mode: "keep", text: "null" },
    });
  });

  it("knows each catalogued setting's kind and draws an unknown one as JSON", () => {
    expect(kindOf("allow_downgrade_default")).toBe("bool");
    expect(kindOf("a_setting_added_later")).toBe("json");
    expect(unitOf("chat_total_timeout_ms")).toBe("ms");
    expect(unitOf("monthly_token_budget")).toBe("tokens");
    expect(unitOf("default_quality")).toBeUndefined();
    expect(Object.keys(SETTING_KINDS)).toHaveLength(15);
  });
});
