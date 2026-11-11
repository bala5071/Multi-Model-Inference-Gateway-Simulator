// The typed settings form's pure part (docs/55): its controls read and write the same draft as
// the JSON table under "Advanced" (`derive.ts`'s Draft), so both views make one change, which the
// gateway's dry run judges. Nothing here decides what is allowed.

import { formatValue, type Draft, type Field } from "./derive";
import { kindOf, unitOf } from "./settingKinds";
import type { Json, OrganizationSettings } from "./types";

/** The arrow between a value before and after, and an alias and its tier (written as its code
 * point: the character check keeps source ASCII). */
export const ARROW = String.fromCodePoint(0x2192);

/** A draft field's value as JSON, or undefined when its text is not JSON (yet). */
export function fieldValue(field: Field | undefined): Json | undefined {
  if (field === undefined) return undefined;
  try {
    return JSON.parse(field.text) as Json;
  } catch {
    return undefined;
  }
}

/**
 * The draft with `name`'s text replaced: kept when the text is back to what was read, set
 * otherwise. A typed control writes its value as JSON; a number field its text as typed.
 */
export function withText(draft: Draft, initial: Draft, name: string, text: string): Draft {
  const before = initial[name]?.text;
  return { ...draft, [name]: { mode: text === before ? "keep" : "set", text } };
}

export function withValue(draft: Draft, initial: Draft, name: string, value: Json): Draft {
  return withText(draft, initial, name, JSON.stringify(value));
}

/** Back to the inherited value: the gateway's `unset`. */
export function withUnset(draft: Draft, name: string): Draft {
  const field = draft[name];
  return { ...draft, [name]: { mode: "unset", text: field?.text ?? "null" } };
}

/** The field as it was read. */
export function withUndo(draft: Draft, initial: Draft, name: string): Draft {
  const field = initial[name];
  return field === undefined ? draft : { ...draft, [name]: field };
}

/** A value in words: On and Off, a number with its unit, a list joined, "not set" for none. */
export function displayValue(name: string, value: Json | undefined): string {
  if (value === undefined || value === null) return "not set";
  if (typeof value === "boolean") return value ? "On" : "Off";
  if (typeof value === "number") {
    const unit = unitOf(name);
    return unit === undefined ? String(value) : `${String(value)} ${unit}`;
  }
  if (kindOf(name) === "providers" && value === "all") return "All providers";
  if (Array.isArray(value)) return value.length === 0 ? "none" : value.map(String).join(", ");
  if (typeof value === "object") {
    const entries = Object.entries(value);
    return entries.length === 0
      ? "none"
      : entries.map(([k, v]) => `${k} ${ARROW} ${formatValue(v)}`).join(", ");
  }
  return value;
}

export interface ChangedRow {
  name: string;
  before: string;
  after: string;
}

/** Each changed setting with its value before and after, in the draft's order. */
export function changedRows(draft: Draft, initial: Draft): ChangedRow[] {
  return Object.entries(draft).flatMap(([name, field]) => {
    if (field.mode === "keep") return [];
    const before = displayValue(name, fieldValue(initial[name]));
    if (field.mode === "unset") return [{ name, before, after: "the inherited value" }];
    const value = fieldValue(field);
    return [{ name, before, after: value === undefined ? field.text : displayValue(name, value) }];
  });
}

/**
 * An organization's draft: each setting it may cap (the platform's caps name them) and each it
 * has set, with its stored value, or null where it set none.
 */
export function organizationDraft(settings: OrganizationSettings): Draft {
  const names = [...Object.keys(settings.platform_caps)];
  for (const name of Object.keys(settings.settings)) if (!names.includes(name)) names.push(name);
  const draft: Draft = {};
  for (const name of names) {
    draft[name] = { mode: "keep", text: JSON.stringify(settings.settings[name] ?? null) };
  }
  return draft;
}
