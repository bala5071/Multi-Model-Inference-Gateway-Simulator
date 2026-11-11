import { THEME_PREFERENCES, useTheme, type ThemePreference } from "../hooks/useTheme";
import type { Capabilities, HeldRole, SessionUser } from "../console/types";

/** A role as the gateway names it, with its scope: shown as text, never read for a decision. */
export function roleText(held: HeldRole): string {
  return held.scope_type === "platform"
    ? held.role
    : `${held.role} (${held.scope_type} ${held.scope_id})`;
}

const PREFERENCE_WORDS: Record<ThemePreference, string> = {
  system: "System",
  light: "Light",
  dark: "Dark",
};

/**
 * The page's one header: who is logged in, in which organization, their roles, the theme
 * (System, Light or Dark: docs/52, Q7), and Log out.
 */
export function Header({
  user,
  capabilities,
  simulated,
  onLogout,
}: {
  user: SessionUser | null;
  capabilities: Capabilities | null;
  simulated: boolean;
  onLogout: (() => void) | null;
}) {
  const { preference, setPreference } = useTheme();
  const roles = capabilities?.roles.map(roleText) ?? [];
  return (
    <header className="top">
      <h1>Gateway</h1>
      {simulated && <span className="mode-badge">Simulated</span>}
      {user !== null && (
        <span className="muted who" aria-label="Logged in as">
          {user.username} · organization {user.organization_id}
          {roles.length > 0 && ` · ${roles.join(", ")}`}
        </span>
      )}
      <label className="theme-choice">
        Theme
        <select
          value={preference}
          onChange={(event) => {
            const next = THEME_PREFERENCES.find((p) => p === event.target.value);
            if (next !== undefined) setPreference(next);
          }}
        >
          {THEME_PREFERENCES.map((p) => (
            <option key={p} value={p}>
              {PREFERENCE_WORDS[p]}
            </option>
          ))}
        </select>
      </label>
      {onLogout !== null && (
        <button type="button" onClick={onLogout}>
          Log out
        </button>
      )}
    </header>
  );
}
