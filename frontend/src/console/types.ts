// The admin endpoints the console reads and writes, mirrored by hand from the gateway's
// answers (docs/27, docs/40, docs/42). The gateway decides everything; these are only shapes.

export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };

export interface SessionUser {
  user_id: string;
  username: string;
  organization_id: string;
  must_change_password: boolean;
}

/** `POST /v1/auth/login`. */
export interface LoginAnswer {
  user: SessionUser;
  csrf_token: string;
}

/** `GET /v1/auth/me`. */
export interface MeAnswer extends LoginAnswer {
  step_up_until_ms: number | null;
}

/** One setting of an application, as `GET .../applications/{app}/settings` explains it. */
export interface EffectiveSetting {
  value: Json;
  source: string;
  capped_by: string | null;
  stored: Json;
  caps: Record<string, Json>;
}

export interface ApplicationSettings {
  application_id: string;
  organization_id: string;
  mode: { organization: string; application: string; effective: string };
  version: number;
  settings: Record<string, Json>;
  effective: Record<string, EffectiveSetting>;
  ignored_aliases: string[];
}

export interface OrganizationSettings {
  organization_id: string;
  mode: string;
  version: number;
  settings: Record<string, Json>;
  platform_caps: Record<string, Record<string, Json>>;
}

/** `POST .../settings/validate`: the dry run of a change. */
export interface Validation {
  valid: boolean;
  code: string | null;
  message: string | null;
  invalid_setting: string | null;
  refusals: { setting: string; level: string }[];
  changed: string[];
  settings: Record<string, Json>;
  loosens: string[];
  step_up_required: boolean;
  effective?: Record<string, Json>;
}

/** A stored change: `PUT`, revert and a preset. */
export interface Change {
  version: number;
  changed: string[];
  settings: Record<string, Json>;
}

export interface VersionSummary {
  version: number;
  changed_fields: string[];
  changed_by_user_id: string | null;
  via: string;
  changed_at_ms: number;
}

export interface VersionPage {
  versions: VersionSummary[];
  next_after: number | null;
}

export interface ApplicationSummary {
  application_id: string;
  name: string;
  organization_id: string;
  plan: string;
}

export interface ApplicationPage {
  applications: ApplicationSummary[];
  next: string | null;
}

export interface PresetList {
  presets: Record<string, Record<string, Json>>;
}

/** The body of every refusal: `{"error": {"code", "message"}}`. */
export interface ErrorBody {
  error: { code: string; message: string };
}
