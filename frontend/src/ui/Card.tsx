import { useId, type ReactNode } from "react";

/**
 * A card: a section named by its heading (or by `label` where a test or a screen reader already
 * knows it by another name), its actions beside the heading.
 */
export function Card({
  title,
  label,
  actions,
  children,
  className,
}: {
  title: ReactNode;
  label?: string;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  const id = useId();
  return (
    <section
      className={className === undefined ? "panel card" : `panel card ${className}`}
      aria-label={label}
      aria-labelledby={label === undefined ? id : undefined}
    >
      <div className="card-head">
        <h3 id={id}>{title}</h3>
        {actions !== undefined && <div className="card-actions">{actions}</div>}
      </div>
      {children}
    </section>
  );
}

/** Stat tiles: a label, a value and a note each, as a description list. */
export function StatTiles({ children }: { children: ReactNode }) {
  return <dl className="facts stat-tiles">{children}</dl>;
}

export function StatTile({
  label,
  value,
  note,
}: {
  label: ReactNode;
  value: ReactNode;
  note?: ReactNode;
}) {
  return (
    <div className="stat-tile">
      <dt>{label}</dt>
      <dd>
        <span className="stat-value">{value}</span>
        {note !== undefined && <span className="stat-note">{note}</span>}
      </dd>
    </div>
  );
}
