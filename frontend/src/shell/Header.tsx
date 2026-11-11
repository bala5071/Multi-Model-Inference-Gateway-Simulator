import { useTheme } from "../hooks/useTheme";
import type { Capabilities, HeldRole, SessionUser } from "../console/types";

/** A role as the gateway names it, with its scope: shown as text, never read for a decision. */
export function roleText(held: HeldRole): string {
  return held.scope_type === "platform"
    ? held.role
    : `${held.role} (${held.scope_type} ${held.scope_id})`;
}

/** The page's one header: who is logged in, in which organization, their roles, and Log out. */
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
  const { theme, toggle } = useTheme();
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
      <button type="button" onClick={toggle}>
        {theme === "dark" ? "Light theme" : "Dark theme"}
      </button>
      {onLogout !== null && (
        <button type="button" onClick={onLogout}>
          Log out
        </button>
      )}
    </header>
  );
}
