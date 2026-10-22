import { useEffect, useState } from "react";

function read<T extends string>(tabs: readonly T[], fallback: T): T {
  const name = window.location.hash.replace(/^#\/?/, "");
  return tabs.find((tab) => tab === name) ?? fallback;
}

/**
 * The selected tab, kept in the URL's hash (`#/latency`), so that it survives a reload
 * and can be linked to without the server knowing any route but /dashboard.
 */
export function useHashTab<T extends string>(
  tabs: readonly T[],
  fallback: T,
): [T, (tab: T) => void] {
  const [tab, setTab] = useState<T>(() => read(tabs, fallback));

  useEffect(() => {
    const onChange = () => {
      setTab(read(tabs, fallback));
    };
    window.addEventListener("hashchange", onChange);
    return () => {
      window.removeEventListener("hashchange", onChange);
    };
  }, [tabs, fallback]);

  return [
    tab,
    (next) => {
      window.location.hash = `/${next}`;
      setTab(next);
    },
  ];
}
