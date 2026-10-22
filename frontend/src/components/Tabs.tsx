export interface TabsProps<T extends string> {
  tabs: readonly { id: T; label: string }[];
  selected: T;
  onSelect: (tab: T) => void;
}

export function Tabs<T extends string>({ tabs, selected, onSelect }: TabsProps<T>) {
  return (
    <div className="tabs" role="tablist" aria-label="Dashboard sections">
      {tabs.map((tab) => (
        <button
          key={tab.id}
          type="button"
          role="tab"
          id={`tab-${tab.id}`}
          aria-selected={tab.id === selected}
          aria-controls="tab-panel"
          tabIndex={tab.id === selected ? 0 : -1}
          onClick={() => {
            onSelect(tab.id);
          }}
          onKeyDown={(event) => {
            const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
            if (step === 0) return;
            const index = tabs.findIndex((t) => t.id === selected);
            const next = tabs[(index + step + tabs.length) % tabs.length];
            if (next !== undefined) {
              onSelect(next.id);
              document.getElementById(`tab-${next.id}`)?.focus();
            }
          }}
        >
          {tab.label}
        </button>
      ))}
    </div>
  );
}
