/**
 * Stands in for a metric the gateway cannot observe behind this backend. It is drawn
 * instead of the metric, never beside a zero.
 */
export function Unavailable({ what }: { what: string }) {
  return (
    <div className="unavailable" role="note">
      <strong>{what}</strong>
      <span>Not available in this backend</span>
    </div>
  );
}
