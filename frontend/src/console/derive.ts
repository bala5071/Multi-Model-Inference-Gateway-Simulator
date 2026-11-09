// Pure transforms from the gateway's answers to what the console's screens show. No rule
// about settings lives here: what is valid, what is capped and who may write are the
// gateway's answers, shown as they come.

import type { WriteProbe } from "./api";
import type {
  ApplicationSettings,
  ApplicationSummary,
  Json,
  OrganizationSettings,
  Validation,
  VersionSummary,
} from "./types";

/** Write controls are shown only when the gateway said yes; never on a refusal or a doubt. */
export function canWrite(probe: WriteProbe | null): boolean {
  return probe === "allowed";
}

/** A value as the console prints it: JSON, compact; nothing set is "not set". */
export function formatValue(value: Json | undefined): string {
  if (value === undefined || value === null) return "not set";
  return JSON.stringify(value);
}

export interface SettingRow {
  name: string;
  value: string;
  source: string;
  cappedBy: string;
  stored: string;
  caps: string;
}

function formatCaps(caps: Record<string, Json>): string {
  const entries = Object.entries(caps);
  if (entries.length === 0) return "none";
  return entries.map(([level, value]) => `${level}: ${formatValue(value)}`).join("; ");
}

/** Every setting of an application, in the gateway's order, with where its value came from. */
export function settingRows(settings: ApplicationSettings): SettingRow[] {
  return Object.entries(settings.effective).map(([name, found]) => ({
    name,
    value: formatValue(found.value),
    source: found.source,
    cappedBy: found.capped_by ?? "-",
    stored: name in settings.settings ? formatValue(found.stored) : "not set",
    caps: formatCaps(found.caps),
  }));
}

export interface CapRow {
  name: string;
  organization: string;
  platform: string;
}

/** An organization's caps: what it set, beside the platform's cap over each it may set. */
export function capRows(settings: OrganizationSettings): CapRow[] {
  const names = [...Object.keys(settings.platform_caps)];
  for (const name of Object.keys(settings.settings)) if (!names.includes(name)) names.push(name);
  return names.map((name) => ({
    name,
    organization: formatValue(settings.settings[name]),
    platform: formatCaps(settings.platform_caps[name] ?? {}),
  }));
}

export interface LimitRow {
  name: string;
  applications: string[];
}

/**
 * For each cap the organization set, the applications it holds down now: those whose value in
 * force the gateway says came from the organization (`source`: the cap is the value, nothing
 * lower was set) or was lowered by it (`capped_by`). Both are the gateway's words.
 */
export function limitRows(
  organization: OrganizationSettings,
  applications: readonly { summary: ApplicationSummary; settings: ApplicationSettings }[],
): LimitRow[] {
  return Object.keys(organization.settings).map((name) => ({
    name,
    applications: applications
      .filter((a) => {
        const found = a.settings.effective[name];
        return found?.source === "organization" || found?.capped_by === "organization";
      })
      .map((a) => a.summary.name),
  }));
}

/** Milliseconds since the epoch as a UTC time, to the second. */
export function formatTime(ms: number): string {
  return `${new Date(ms).toISOString().slice(0, 19).replace("T", " ")} UTC`;
}

export interface VersionRow {
  version: number;
  fields: string;
  by: string;
  via: string;
  when: string;
}

export function versionRows(versions: readonly VersionSummary[]): VersionRow[] {
  return versions.map((v) => ({
    version: v.version,
    fields: v.changed_fields.length === 0 ? "none" : v.changed_fields.join(", "),
    by: v.changed_by_user_id ?? "the operator (CLI)",
    via: v.via,
    when: formatTime(v.changed_at_ms),
  }));
}

/** The edit form: for each setting, keep it, set it to the JSON typed, or unset it. */
export type FieldMode = "keep" | "set" | "unset";
export interface Field {
  mode: FieldMode;
  text: string;
}
export type Draft = Record<string, Field>;

export function initialDraft(settings: ApplicationSettings): Draft {
  const draft: Draft = {};
  for (const [name, found] of Object.entries(settings.effective)) {
    const shown = name in settings.settings ? found.stored : found.value;
    draft[name] = { mode: "keep", text: JSON.stringify(shown) };
  }
  return draft;
}

export interface DraftChange {
  set: Record<string, Json>;
  unset: string[];
  /** Fields whose text is not JSON at all; the gateway judges everything else. */
  unparsed: string[];
  empty: boolean;
}

/** The change a draft asks for, as the gateway's `set` and `unset`. */
export function draftChange(draft: Draft): DraftChange {
  const set: Record<string, Json> = {};
  const unset: string[] = [];
  const unparsed: string[] = [];
  for (const [name, field] of Object.entries(draft)) {
    if (field.mode === "unset") unset.push(name);
    if (field.mode !== "set") continue;
    try {
      set[name] = JSON.parse(field.text) as Json;
    } catch {
      unparsed.push(name);
    }
  }
  return { set, unset, unparsed, empty: Object.keys(set).length === 0 && unset.length === 0 };
}

export type Verdict =
  | { kind: "nothing" }
  | { kind: "unparsed"; fields: string[] }
  | { kind: "checking" }
  | { kind: "valid"; changed: string[]; stepUp: boolean; effective: [string, string][] }
  | { kind: "invalid"; message: string; setting: string | null; refusals: string[] };

/** What the form says about a change, from the gateway's dry run of it. */
export function verdict(change: DraftChange, validation: Validation | null): Verdict {
  if (change.unparsed.length > 0) return { kind: "unparsed", fields: change.unparsed };
  if (change.empty) return { kind: "nothing" };
  if (validation === null) return { kind: "checking" };
  if (!validation.valid) {
    return {
      kind: "invalid",
      message: validation.message ?? "The gateway refused the change.",
      setting: validation.invalid_setting,
      refusals: validation.refusals.map((r) => `${r.setting} (looser than the ${r.level}'s cap)`),
    };
  }
  return {
    kind: "valid",
    changed: validation.changed,
    stepUp: validation.step_up_required,
    effective: Object.entries(validation.effective ?? {})
      .filter(([name]) => validation.changed.includes(name))
      .map(([name, value]) => [name, formatValue(value)]),
  };
}

/** Saving is offered only for a change the gateway's dry run accepted and that changes something. */
export function canSave(v: Verdict): boolean {
  return v.kind === "valid" && v.changed.length > 0;
}

export interface PresetRow {
  name: string;
  values: [string, string][];
}

export function presetRows(presets: Record<string, Record<string, Json>>): PresetRow[] {
  return Object.entries(presets).map(([name, values]) => ({
    name,
    values: Object.entries(values).map(([setting, value]) => [setting, formatValue(value)]),
  }));
}

/** The console's two screens, in the URL's hash: `#/application/<id>` and `#/organization`. */
export type Route =
  { screen: "application"; applicationId: string | null } | { screen: "organization" };

export function parseRoute(hash: string): Route {
  const parts = hash.replace(/^#\/?/, "").split("/");
  if (parts[0] === "organization") return { screen: "organization" };
  const id = parts[0] === "application" && parts[1] ? decodeURIComponent(parts[1]) : null;
  return { screen: "application", applicationId: id };
}

export function routeHash(route: Route): string {
  if (route.screen === "organization") return "#/organization";
  return route.applicationId === null
    ? "#/application"
    : `#/application/${encodeURIComponent(route.applicationId)}`;
}

/** The words for a refusal the console shows; the gateway's own message otherwise. */
export function refusalText(code: string, message: string): string {
  if (code === "forbidden") return "Not permitted, or not found.";
  if (code === "session_required") return "Your session has ended. Log in again.";
  if (code === "settings_version_conflict") {
    return "The settings changed since they were read. They have been read again: check them and make the change from there.";
  }
  return message;
}
