export interface MeterProps {
  label: string;
  /** 0..1; drawn clamped, so an overshoot still fills the bar and no more. */
  ratio: number;
  level?: "ok" | "warning" | "exhausted";
}

/** A horizontal bar for a share of something. The label says what; colour only repeats it. */
export function Meter({ label, ratio, level = "ok" }: MeterProps) {
  const percent = Math.round(Math.min(1, Math.max(0, ratio)) * 100);
  return (
    <div
      className={`meter meter-${level}`}
      role="meter"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={percent}
    >
      <div className="meter-fill" style={{ width: `${percent}%` }} />
    </div>
  );
}
