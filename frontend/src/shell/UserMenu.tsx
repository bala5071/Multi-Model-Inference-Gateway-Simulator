import { Check, ChevronDown, LogOut } from "lucide-react";
import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
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
 * The account menu (docs/54), to the WAI-ARIA menu button pattern: the button opens it (click,
 * Enter, Space, ArrowDown to the first item, ArrowUp to the last); in it, the arrow keys, Home and
 * End move between items, Escape closes it and gives the focus back to the button, Tab closes it.
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
  const [open, setOpen] = useState(false);
  const [focusIndex, setFocusIndex] = useState(0);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const items = useRef<(HTMLButtonElement | null)[]>([]);
  const menuId = useId();
  const whoId = useId();
  const count = THEME_PREFERENCES.length + 1;

  useEffect(() => {
    if (open) items.current[focusIndex]?.focus();
  }, [open, focusIndex]);

  useEffect(() => {
    if (!open) return;
    const outside = (event: MouseEvent) => {
      const target = event.target as Node;
      if (!menuRef.current?.contains(target) && !buttonRef.current?.contains(target)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", outside);
    return () => {
      document.removeEventListener("mousedown", outside);
    };
  }, [open]);

  function openAt(index: number) {
    setFocusIndex(index);
    setOpen(true);
  }

  function close(returnFocus: boolean) {
    setOpen(false);
    if (returnFocus) buttonRef.current?.focus();
  }

  function onButtonKey(event: KeyboardEvent<HTMLButtonElement>) {
    if (event.key === "ArrowDown" || event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openAt(0);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      openAt(count - 1);
    }
  }

  function onMenuKey(event: KeyboardEvent<HTMLDivElement>) {
    const moves: Record<string, number> = {
      ArrowDown: (focusIndex + 1) % count,
      ArrowUp: (focusIndex - 1 + count) % count,
      Home: 0,
      End: count - 1,
    };
    const next = moves[event.key];
    if (next !== undefined) {
      event.preventDefault();
      setFocusIndex(next);
    } else if (event.key === "Escape") {
      event.preventDefault();
      close(true);
    } else if (event.key === "Tab") {
      close(false);
    }
  }

  return (
    <div className="user-menu">
      <button
        ref={buttonRef}
        type="button"
        className="user-menu-button quiet"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        onClick={() => {
          if (open) close(false);
          else openAt(0);
        }}
        onKeyDown={onButtonKey}
      >
        <span className="avatar" aria-hidden="true">
          {initials(username)}
        </span>
        {/* The name is "Account: <user>": the words a screen reader hears contain what is seen. */}
        <span className="visually-hidden">Account:</span>{" "}
        <span className="user-menu-name">{username}</span>
        <ChevronDown aria-hidden="true" focusable="false" size={16} />
      </button>
      {open && (
        <div className="menu-popup">
          <div id={whoId} className="menu-who">
            <strong>{username}</strong>
            <span>Organization {organizationId}</span>
            {roles.length > 0 && <span>{roles.join(", ")}</span>}
          </div>
          <div
            ref={menuRef}
            id={menuId}
            role="menu"
            aria-label="Account"
            aria-describedby={whoId}
            tabIndex={-1}
            onKeyDown={onMenuKey}
          >
            <div role="group" aria-label="Theme" className="menu-group">
              <div className="menu-group-label" aria-hidden="true">
                Theme
              </div>
              {THEME_PREFERENCES.map((p, index) => (
                <button
                  key={p}
                  ref={(element) => {
                    items.current[index] = element;
                  }}
                  type="button"
                  role="menuitemradio"
                  aria-checked={preference === p}
                  tabIndex={-1}
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
              ref={(element) => {
                items.current[THEME_PREFERENCES.length] = element;
              }}
              type="button"
              role="menuitem"
              tabIndex={-1}
              className="menu-item"
              onClick={() => {
                setOpen(false);
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
