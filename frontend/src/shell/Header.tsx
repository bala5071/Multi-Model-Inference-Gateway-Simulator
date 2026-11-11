import { Menu, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { THEME_PREFERENCES, useTheme, type ThemePreference } from "../hooks/useTheme";
import type { Capabilities, HeldRole, SessionUser } from "../console/types";
import { Brand } from "./Brand";
import { routeHash, type Crumb } from "./routes";
import { UserMenu } from "./UserMenu";

/** A role as words: the gateway's name with its underscores as spaces, never one named here. */
export function roleWords(role: string): string {
  return role.replaceAll("_", " ");
}

/** A role the gateway names, with its scope: shown as words, never read for a decision. */
export function roleText(held: HeldRole): string {
  const words = roleWords(held.role);
  return held.scope_type === "platform" ? words : `${words} (${held.scope_type} ${held.scope_id})`;
}

const PREFERENCE_WORDS: Record<ThemePreference, string> = {
  system: "System",
  light: "Light",
  dark: "Dark",
};

/** The theme, where there is no account menu to hold it (logged out, no accounts). */
function ThemeSelect() {
  const { preference, setPreference } = useTheme();
  return (
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
  );
}

function Breadcrumbs({ trail }: { trail: Crumb[] }) {
  return (
    <nav className="breadcrumbs" aria-label="Breadcrumb">
      <ol>
        {trail.map((crumb, index) => {
          const isLast = index === trail.length - 1;
          return (
            <li key={`${crumb.label}-${index.toString()}`}>
              {crumb.route !== null && !isLast ? (
                <a href={routeHash(crumb.route)}>{crumb.label}</a>
              ) : (
                <span aria-current={isLast ? "page" : undefined}>{crumb.label}</span>
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

/**
 * The page's top bar (docs/54). Logged in: the breadcrumbs and the account menu (who, the
 * organization, the roles as words, the theme, Log out). Otherwise the mark and the theme. While a
 * password must be changed it shows only the mark: that screen has its own Log out.
 */
export function Header({
  user,
  capabilities,
  simulated,
  onLogout,
  trail = null,
  withSidebar = false,
  account = true,
  drawer = null,
}: {
  user: SessionUser | null;
  capabilities: Capabilities | null;
  simulated: boolean;
  onLogout: (() => void) | null;
  trail?: Crumb[] | null;
  withSidebar?: boolean;
  account?: boolean;
  /** The navigation, opened in a dialog on narrow screens; drawn only while it is open. */
  drawer?: ReactNode;
}) {
  const roles = capabilities?.roles.map(roleText) ?? [];
  const drawerRef = useRef<HTMLDialogElement>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const closeDrawer = useCallback(() => {
    const dialog = drawerRef.current;
    if (dialog?.open === true) dialog.close();
  }, []);
  // A link followed in the drawer changes the place: the drawer closes.
  const place = trail?.map((c) => c.label).join("/") ?? "";
  useEffect(() => {
    closeDrawer();
  }, [place, closeDrawer]);
  useEffect(() => {
    if (drawerOpen && drawerRef.current?.open === false) drawerRef.current.showModal();
  }, [drawerOpen]);
  // A link followed to the place already shown changes nothing, so it closes the drawer itself
  // (by mouse or by Enter: both are a click on the link).
  useEffect(() => {
    const dialog = drawerRef.current;
    if (dialog === null) return;
    const onClick = (event: MouseEvent) => {
      if (event.target instanceof Element && event.target.closest("a") !== null) closeDrawer();
    };
    dialog.addEventListener("click", onClick);
    return () => {
      dialog.removeEventListener("click", onClick);
    };
  }, [closeDrawer, drawer]);
  const showAccount = account && user !== null && onLogout !== null;
  return (
    <header className={`top ${withSidebar ? "top-with-sidebar" : ""}`}>
      {drawer !== null && (
        <button
          type="button"
          className="drawer-button quiet"
          aria-label="Open the navigation"
          onClick={() => {
            setDrawerOpen(true);
          }}
        >
          <Menu aria-hidden="true" focusable="false" size={20} />
        </button>
      )}
      {withSidebar ? null : <Brand />}
      {trail !== null && <Breadcrumbs trail={trail} />}
      <div className="top-spacer" />
      {simulated && <span className="mode-badge">Simulated</span>}
      {showAccount ? (
        <UserMenu
          username={user.username}
          organizationId={user.organization_id}
          roles={roles}
          onLogout={onLogout}
        />
      ) : (
        account && <ThemeSelect />
      )}
      {drawer !== null && (
        <dialog
          ref={drawerRef}
          className="drawer"
          aria-label="Navigation"
          onClose={() => {
            setDrawerOpen(false);
          }}
        >
          <div className="drawer-head">
            <Brand />
            <button
              type="button"
              className="quiet"
              aria-label="Close the navigation"
              onClick={closeDrawer}
            >
              <X aria-hidden="true" focusable="false" size={20} />
            </button>
          </div>
          {drawerOpen && drawer}
        </dialog>
      )}
    </header>
  );
}
