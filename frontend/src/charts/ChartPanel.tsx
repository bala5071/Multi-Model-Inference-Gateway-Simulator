import { useId, useState } from "react";
import { useTheme } from "../hooks/useTheme";
import { TimeChart, type ChartLine, type ReferenceLine } from "./TimeChart";
import {
  bucketSize,
  downsample,
  formatAge,
  formatValue,
  largest,
  secondsBefore,
  yScale,
  type Columns,
  type Reducer,
} from "./transforms";

export interface PanelLine extends ChartLine {
  /** How this line's values are combined when several seconds share one drawn point. */
  reducer: Reducer;
}

export interface ChartPanelProps {
  title: string;
  /** Printed on the y axis, in the legend and in the table. */
  unit: string;
  /** What one point means; shown under the title. */
  note?: string;
  /** Clock times in ms and one column per line, at `stepMs` per row. */
  columns: Columns;
  lines: PanelLine[];
  /** The clock time that counts as now, in ms. */
  nowMs: number;
  stepMs: number;
  reference?: ReferenceLine | null;
  /** A fixed top for the y axis, such as a capacity. */
  fixedMax?: number | null;
  maxPoints?: number;
}

const TABLE_ROWS = 20;

/**
 * One chart with its title, units, pause button and table view.
 *
 * Pausing holds the data as it was when the button was pressed; the stream keeps running
 * underneath and the chart jumps to the present when resumed.
 */
export function ChartPanel(props: ChartPanelProps) {
  const [held, setHeld] = useState<{ columns: Columns; nowMs: number } | null>(null);
  const [showTable, setShowTable] = useState(false);
  const { theme } = useTheme();
  const titleId = useId();
  const { title, unit, note, lines, stepMs, reference = null, fixedMax = null } = props;
  const maxPoints = props.maxPoints ?? 300;

  const shown = held ?? { columns: props.columns, nowMs: props.nowMs };
  const size = bucketSize(shown.columns.xs.length, maxPoints);
  const drawn = downsample(
    shown.columns,
    maxPoints,
    lines.map((line) => line.reducer),
  );
  const xs = secondsBefore(drawn.xs, shown.nowMs);
  const scale = yScale(largest(drawn.ys), reference?.value ?? null, fixedMax);
  const empty = largest(drawn.ys) === null;
  const perPoint = (stepMs * size) / 1000;

  return (
    <section className="panel" aria-labelledby={titleId}>
      <header className="panel-head">
        <div>
          <h4 id={titleId}>
            {title} <span className="unit">({unit})</span>
          </h4>
          <p className="muted">
            {note}
            {size > 1 && ` Each point covers ${perPoint} s.`}
            {reference !== null &&
              !scale.referenceShown &&
              ` ${reference.label} is above the range drawn.`}
          </p>
        </div>
        <div className="panel-actions">
          <button
            type="button"
            aria-pressed={showTable}
            onClick={() => {
              setShowTable(!showTable);
            }}
          >
            Table
          </button>
          <button
            type="button"
            aria-pressed={held !== null}
            onClick={() => {
              setHeld(held === null ? { columns: props.columns, nowMs: props.nowMs } : null);
            }}
          >
            {held === null ? "Pause" : "Resume"}
          </button>
        </div>
      </header>
      {held !== null && <p className="paused-note">Paused. New data is not shown.</p>}
      {empty ? (
        <p className="empty">No data in this window yet.</p>
      ) : (
        <TimeChart
          xs={xs}
          ys={drawn.ys}
          lines={lines}
          unit={unit}
          yMax={scale.max}
          reference={scale.referenceShown ? reference : null}
          theme={theme}
        />
      )}
      {showTable && (
        <table className="data-table">
          <caption>
            {title}, newest {TABLE_ROWS} points, in {unit}
          </caption>
          <thead>
            <tr>
              <th scope="col">Time before now</th>
              {lines.map((line) => (
                <th key={line.label} scope="col">
                  {line.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {xs
              .map((x, row) => ({ x, row }))
              .slice(-TABLE_ROWS)
              .reverse()
              .map(({ x, row }) => (
                <tr key={x}>
                  <th scope="row">{formatAge(x)}</th>
                  {lines.map((line, index) => (
                    <td key={line.label}>{formatValue(drawn.ys[index]?.[row] ?? null)}</td>
                  ))}
                </tr>
              ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
