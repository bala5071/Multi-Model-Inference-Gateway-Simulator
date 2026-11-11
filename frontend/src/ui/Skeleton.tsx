/** Placeholder lines while something loads: hidden from screen readers, which hear "Loading...". */
export function Skeleton({ lines = 3, label = "Loading…" }: { lines?: number; label?: string }) {
  return (
    <div className="skeleton-block">
      <span className="visually-hidden" role="status">
        {label}
      </span>
      {Array.from({ length: lines }, (_, i) => (
        <div key={i} className="skeleton" aria-hidden="true" />
      ))}
    </div>
  );
}

/** Three placeholder rows for a table that is loading. */
export function SkeletonRows({ columns }: { columns: number }) {
  return (
    <>
      {[0, 1, 2].map((row) => (
        <tr key={row} aria-hidden="true">
          {Array.from({ length: columns }, (_, i) => (
            <td key={i}>
              <div className="skeleton" />
            </td>
          ))}
        </tr>
      ))}
    </>
  );
}
