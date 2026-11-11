import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

/** The theme in force: what the stylesheet draws. */
export type Theme = "light" | "dark";
/** What the person chose (docs/52, Q7): the system's setting by default, or one of the two. */
export type ThemePreference = "system" | "light" | "dark";

export const THEME_PREFERENCES: readonly ThemePreference[] = ["system", "light", "dark"];

interface ThemeContext {
  theme: Theme;
  preference: ThemePreference;
  setPreference: (next: ThemePreference) => void;
}

// The same key as before: a saved "light" or "dark" keeps its meaning. Only the word is kept.
const STORAGE_KEY = "gateway-dashboard-theme";
const DARK_QUERY = "(prefers-color-scheme: dark)";

const Context = createContext<ThemeContext>({
  theme: "light",
  preference: "system",
  setPreference: () => undefined,
});

function savedPreference(): ThemePreference {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved === "light" || saved === "dark" || saved === "system") return saved;
  } catch {
    // Storage can be blocked; the system setting applies.
  }
  return "system";
}

function systemTheme(): Theme {
  return window.matchMedia(DARK_QUERY).matches ? "dark" : "light";
}

/** The theme a preference gives, with the system's setting as it is now. */
export function resolveTheme(preference: ThemePreference, system: Theme): Theme {
  return preference === "system" ? system : preference;
}

/**
 * The saved preference (System unless one was chosen), and the theme it gives, stamped on
 * <html> for the stylesheet. With System, a change of the operating system's setting applies at
 * once.
 */
export function ThemeProvider({ children }: { children: ReactNode }) {
  const [preference, setPreferenceState] = useState<ThemePreference>(savedPreference);
  const [system, setSystem] = useState<Theme>(systemTheme);
  const theme = resolveTheme(preference, system);

  useEffect(() => {
    const query = window.matchMedia(DARK_QUERY);
    const onChange = () => {
      setSystem(query.matches ? "dark" : "light");
    };
    query.addEventListener("change", onChange);
    return () => {
      query.removeEventListener("change", onChange);
    };
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  const value = useMemo(
    () => ({
      theme,
      preference,
      setPreference: (next: ThemePreference) => {
        try {
          localStorage.setItem(STORAGE_KEY, next);
        } catch {
          // Not saved; it still applies until the page is closed.
        }
        setPreferenceState(next);
      },
    }),
    [theme, preference],
  );
  return <Context value={value}>{children}</Context>;
}

export function useTheme(): ThemeContext {
  return useContext(Context);
}
