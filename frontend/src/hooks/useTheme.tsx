import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

export type Theme = "light" | "dark";

interface ThemeContext {
  theme: Theme;
  toggle: () => void;
}

const STORAGE_KEY = "gateway-dashboard-theme";

const Context = createContext<ThemeContext>({ theme: "light", toggle: () => undefined });

function initialTheme(): Theme {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved === "light" || saved === "dark") return saved;
  } catch {
    // Storage can be blocked; the system setting still applies.
  }
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

/** The viewer's saved choice, else the system's. Stamped on <html> for the stylesheet. */
export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useState<Theme>(initialTheme);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  const value = useMemo(
    () => ({
      theme,
      toggle: () => {
        const next = theme === "dark" ? "light" : "dark";
        try {
          localStorage.setItem(STORAGE_KEY, next);
        } catch {
          // Not saved; it still applies until the page is closed.
        }
        setTheme(next);
      },
    }),
    [theme],
  );
  return <Context value={value}>{children}</Context>;
}

export function useTheme(): ThemeContext {
  return useContext(Context);
}
