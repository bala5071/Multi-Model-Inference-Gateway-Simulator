// Each setting's kind, for the typed settings form (docs/55): which control draws it. A copy of
// the names and kinds of the gateway's CATALOGUE (gateway/settings.py), pinned to it by
// tests/test_dashboard.py. Only the kind is copied, never a bound or a cap: those are the
// gateway's answers. A setting missing here is drawn as a JSON field, so a new one never breaks
// the page.

export type SettingKind = "bool" | "int" | "tiers" | "aliases" | "quality" | "providers";

export const SETTING_KINDS: Readonly<Record<string, SettingKind>> = {
  allowed_tiers: "tiers",
  model_aliases: "aliases",
  default_quality: "quality",
  allow_downgrade_default: "bool",
  requests_per_minute: "int",
  monthly_token_budget: "int",
  max_tokens: "int",
  default_max_tokens: "int",
  generate_timeout_ms: "int",
  chat_first_token_ms: "int",
  chat_total_timeout_ms: "int",
  allowed_providers: "providers",
  max_hosted_attempts: "int",
  retry_invalid_tool_json: "bool",
  hosted_first_token_ms: "int",
};

export function kindOf(name: string): SettingKind | "json" {
  return SETTING_KINDS[name] ?? "json";
}

/** A number setting's unit, read from its name; none where the name says nothing. */
export function unitOf(name: string): string | undefined {
  if (name.endsWith("_ms")) return "ms";
  if (name === "requests_per_minute") return "requests a minute";
  if (name.endsWith("tokens") || name.endsWith("token_budget")) return "tokens";
  if (name.endsWith("_attempts")) return "attempts";
  return undefined;
}
