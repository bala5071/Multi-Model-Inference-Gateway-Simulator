import { Check, ChevronDown, LogOut } from "lucide-react";
import { useId } from "react";
import { useMenuButton } from "../ui/Menu";
import { THEME_PREFERENCES, useTheme, type ThemePreference } from "../hooks/useTheme";

const PREFERENCE_WORDS: Record<ThemePreference, string> = {
  system: "System",
  light: "Light",
  dark: "Dark",
};

/** Up to two letters from a username, for the avatar (decorative: the name is written too). */
export function initials(username: string): string {
  const parts = username.split(/[^A-Za-z0-9]+/).filter((part) => part !== "");
  const letters =
    parts.length >= 2 ? `${parts[0]?.[0] ?? ""}${parts[1]?.[0] ?? ""}` : username.slice(0, 2);
  return letters.toUpperCase();
}

/**
 * The account menu (docs/54), to the WAI-ARIA menu button pattern (`useMenuButton`, docs/55).
 * Who is logged in, the organization and the roles (as the gateway names them, shown as words)
 * describe the menu; the theme is three radio items; Log out is the last item.
 */
export function UserMenu({
  username,
  organizationId,
  roles,
  onLogout,
}: {
  username: string;
  organizationId: string;
  roles: string[];
  onLogout: () => void;
}) {
  const { preference, setPreference } = useTheme();
  const menu = useMenuButton(THEME_PREFERENCES.length + 1);
  const whoId = useId();

  return (
    <div className="user-menu">
      <button type="button" className="user-menu-button quiet" {...menu.buttonProps}>
        <span className="avatar" aria-hidden="true">
          {initials(username)}
        </span>
        {/* The name is "Account: <user>": the words a screen reader hears contain what is seen. */}
        <span className="visually-hidden">Account:</span>{" "}
        <span className="user-menu-name">{username}</span>
        <ChevronDown aria-hidden="true" focusable="false" size={16} />
      </button>
      {menu.open && (
        <div className="menu-popup">
          <div id={whoId} className="menu-who">
            <strong>{username}</strong>
            <span>Organization {organizationId}</span>
            {roles.length > 0 && <span>{roles.join(", ")}</span>}
          </div>
          <div {...menu.menuProps} aria-label="Account" aria-describedby={whoId}>
            <div role="group" aria-label="Theme" className="menu-group">
              <div className="menu-group-label" aria-hidden="true">
                Theme
              </div>
              {THEME_PREFERENCES.map((p, index) => (
                <button
                  key={p}
                  {...menu.itemProps(index)}
                  type="button"
                  role="menuitemradio"
                  aria-checked={preference === p}
                  className="menu-item"
                  onClick={() => {
                    setPreference(p);
                  }}
                >
                  <Check
                    aria-hidden="true"
                    focusable="false"
                    size={16}
                    className={preference === p ? undefined : "invisible"}
                  />
                  {PREFERENCE_WORDS[p]}
                </button>
              ))}
            </div>
            <div role="separator" className="menu-separator" />
            <button
              {...menu.itemProps(THEME_PREFERENCES.length)}
              type="button"
              role="menuitem"
              className="menu-item"
              onClick={() => {
                menu.close(false);
                onLogout();
              }}
            >
              <LogOut aria-hidden="true" focusable="false" size={16} />
              Log out
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
