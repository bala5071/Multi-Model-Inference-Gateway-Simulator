// Pure functions between the API's points and what a chart draws. No DOM, no React.

/** Columns for a chart: one x per row and, per line, one y per row. null is a gap. */
export interface Columns {
  xs: number[];
  ys: (number | null)[][];
}

export type Reducer = "max" | "min" | "mean" | "last";

/**
 * Lay points out on an even time grid, one row per `stepMs` from the first point to the
 * last, with null wherever a step has no point. The API leaves out seconds in which
 * nothing was recorded; drawn without this a line would run straight across them.
 */
export function toColumns<P extends { t_ms: number }>(
  points: readonly P[],
  picks: readonly ((point: P) => number | null)[],
  stepMs: number,
): Columns {
  const xs: number[] = [];
  const ys: (number | null)[][] = picks.map(() => []);
  const first = points[0];
  const last = points.at(-1);
  if (first === undefined || last === undefined) return { xs, ys };

  let next = 0;
  const rows = Math.round((last.t_ms - first.t_ms) / stepMs) + 1;
  for (let row = 0; row < rows; row++) {
    const t = first.t_ms + row * stepMs;
    xs.push(t);
    const point = points[next];
    const here = point !== undefined && point.t_ms <= t + stepMs / 2 ? point : undefined;
    if (here !== undefined) next += 1;
    picks.forEach((pick, line) => ys[line]?.push(here === undefined ? null : pick(here)));
  }
  return { xs, ys };
}

function reduceValues(values: readonly (number | null)[], reducer: Reducer): number | null {
  const present = values.filter((v): v is number => v !== null);
  const last = present.at(-1);
  if (last === undefined) return null;
  switch (reducer) {
    case "max":
      return Math.max(...present);
    case "min":
      return Math.min(...present);
    case "mean":
      return present.reduce((sum, v) => sum + v, 0) / present.length;
    case "last":
      return last;
  }
}

/** How many consecutive rows go into one drawn point so that `rows` fit in `maxPoints`. */
export function bucketSize(rows: number, maxPoints: number): number {
  return Math.max(1, Math.ceil(rows / Math.max(1, maxPoints)));
}

/**
 * Reduce the rows to at most `maxPoints` by combining consecutive rows. Each line has its
 * own reducer (`max` keeps the spikes a p99 line is there to show). A combined row takes
 * the x of its first row, and is null only if every row in it was.
 */
export function downsample(
  columns: Columns,
  maxPoints: number,
  reducers: readonly Reducer[],
): Columns {
  const size = bucketSize(columns.xs.length, maxPoints);
  if (size === 1) return columns;
  const xs: number[] = [];
  const ys: (number | null)[][] = columns.ys.map(() => []);
  for (let start = 0; start < columns.xs.length; start += size) {
    const x = columns.xs[start];
    if (x === undefined) break;
    xs.push(x);
    columns.ys.forEach((line, index) => {
      const reducer = reducers[index] ?? "max";
      ys[index]?.push(reduceValues(line.slice(start, start + size), reducer));
    });
  }
  return { xs, ys };
}

/** Clock times in ms as seconds relative to `nowMs`: 0 is now, -60 a minute ago. */
export function secondsBefore(xsMs: readonly number[], nowMs: number): number[] {
  return xsMs.map((t) => (t - nowMs) / 1000);
}

/** A relative time in seconds as an axis tick: 0 -> "now", -75 -> "-1:15". */
export function formatAge(seconds: number): string {
  const whole = Math.round(Math.abs(seconds));
  if (whole === 0) return "now";
  const minutes = Math.floor(whole / 60);
  const rest = String(whole % 60).padStart(2, "0");
  return `-${minutes}:${rest}`;
}

/** The largest value in any line; null if there is none. */
export function largest(ys: readonly (readonly (number | null)[])[]): number | null {
  let max: number | null = null;
  for (const line of ys) {
    for (const value of line) {
      if (value !== null && (max === null || value > max)) max = value;
    }
  }
  return max;
}

export interface YScale {
  max: number;
  /** False when the reference line is too far above the data to be drawn. */
  referenceShown: boolean;
}

/**
 * The top of a y axis that starts at 0.
 *
 * `fixedMax` (a capacity) always wins: the axis runs to it. Otherwise the axis fits the
 * data, and stretches to include the reference line (an SLO) only if that is within
 * `reach` times the data: a 30 s SLO over 40 ms latencies would flatten the lines into
 * the axis, and the panel then says where the SLO is instead of drawing it.
 */
export function yScale(
  dataMax: number | null,
  reference: number | null,
  fixedMax: number | null,
  reach = 4,
): YScale {
  if (fixedMax !== null) {
    return { max: Math.max(fixedMax, dataMax ?? 0) * 1.05, referenceShown: reference !== null };
  }
  const data = dataMax ?? 0;
  if (reference !== null && (data === 0 || reference <= data * reach)) {
    return { max: Math.max(data, reference) * 1.1, referenceShown: true };
  }
  return { max: data === 0 ? 1 : data * 1.1, referenceShown: false };
}

const WHOLE = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const ONE_DECIMAL = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });

/** A number for reading: thousands separators, and a decimal only where it says something. */
export function formatValue(value: number | null): string {
  if (value === null) return "–";
  return Math.abs(value) >= 100 ? WHOLE.format(value) : ONE_DECIMAL.format(value);
}

export function formatUsd(value: number): string {
  return `$${value.toFixed(value !== 0 && Math.abs(value) < 0.01 ? 6 : 2)}`;
}

/** A window length for a label: "60 s", "5 min", "15 min". */
export function formatWindow(ms: number): string {
  const seconds = Math.round(ms / 1000);
  return seconds % 60 === 0 && seconds >= 120 ? `${seconds / 60} min` : `${seconds} s`;
}

/** A 0..1 ratio as a percentage; null stays null. */
export function toPercent(ratio: number | null): number | null {
  return ratio === null ? null : ratio * 100;
}
