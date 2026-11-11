import type { ReactNode } from "react";

/** The id of a tab, and of the panel the tabs control, from the caller's prefix (`useId`). */
export function tabId(prefix: string, id: string): string {
  return `${prefix}-tab-${id}`;
}
export function panelId(prefix: string): string {
  return `${prefix}-panel`;
}

/**
 * Tabs to the WAI-ARIA tabs pattern: one tab in the Tab order (the selected one), the arrow keys
 * move and select, Home and End go to the first and the last.
 */
export function Tabs<T extends string>({
  label,
  prefix,
  tabs,
  selected,
  onSelect,
}: {
  label: string;
  prefix: string;
  tabs: readonly { id: T; label: string }[];
  selected: T;
  onSelect: (tab: T) => void;
}) {
  function go(index: number) {
    const next = tabs[(index + tabs.length) % tabs.length];
    if (next === undefined) return;
    onSelect(next.id);
    document.getElementById(tabId(prefix, next.id))?.focus();
  }
  return (
    <div className="ui-tabs" role="tablist" aria-label={label}>
      {tabs.map((tab, index) => (
        <button
          key={tab.id}
          type="button"
          role="tab"
          id={tabId(prefix, tab.id)}
          aria-selected={tab.id === selected}
          aria-controls={panelId(prefix)}
          tabIndex={tab.id === selected ? 0 : -1}
          onClick={() => {
            onSelect(tab.id);
          }}
          onKeyDown={(event) => {
            const moves: Record<string, number> = {
              ArrowRight: index + 1,
              ArrowLeft: index - 1,
              Home: 0,
              End: tabs.length - 1,
            };
            const to = moves[event.key];
            if (to === undefined) return;
            event.preventDefault();
            go(to);
          }}
        >
          {tab.label}
        </button>
      ))}
    </div>
  );
}

export function TabPanel({
  prefix,
  selected,
  children,
}: {
  prefix: string;
  selected: string;
  children: ReactNode;
}) {
  return (
    <div
      role="tabpanel"
      id={panelId(prefix)}
      aria-labelledby={tabId(prefix, selected)}
      className="ui-tabpanel"
    >
      {children}
    </div>
  );
}
