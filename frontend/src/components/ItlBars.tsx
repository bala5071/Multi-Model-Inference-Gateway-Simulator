import { formatValue } from "../charts/transforms";

/**
 * One thin bar per token after the first; its height is the wait before that token.
 * Every bar carries its value for hover and for a screen reader, and the scale is stated,
 * since there is no axis.
 */
export function ItlBars({ gaps }: { gaps: readonly number[] }) {
  if (gaps.length === 0) {
    return <p className="muted">A gap needs two tokens; there is nothing to draw yet.</p>;
  }
  const largest = Math.max(...gaps);
  return (
    <figure className="itl">
      <figcaption>
        Gap before each token <span className="unit">(ms)</span>
        <span className="muted">
          {" "}
          {gaps.length} gaps, the tallest bar is {formatValue(largest)} ms
        </span>
      </figcaption>
      <ol className="itl-bars" aria-label="Inter-token gaps in ms">
        {gaps.map((gap, index) => {
          const label = `token ${index + 1}: ${formatValue(gap)} ms`;
          return (
            <li key={index} title={label} aria-label={label}>
              <span
                style={{ height: `${largest === 0 ? 0 : Math.max(2, (gap / largest) * 100)}%` }}
              />
            </li>
          );
        })}
      </ol>
    </figure>
  );
}
