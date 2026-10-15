import { useEffect, useRef } from "react";
import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";
import { formatAge, formatValue } from "./transforms";

export interface ChartLine {
  label: string;
  /** Which categorical slot of the theme draws it. Fixed per line, never by position. */
  slot: 1 | 2;
}

export interface ReferenceLine {
  label: string;
  value: number;
}

export interface TimeChartProps {
  /** Seconds relative to now, ascending; 0 is now. */
  xs: number[];
  ys: (number | null)[][];
  lines: ChartLine[];
  unit: string;
  yMax: number;
  reference?: ReferenceLine | null;
  /** Changing it rebuilds the chart, so that it picks up the theme's colours. */
  theme: string;
  height?: number;
}

function isGap(value: number | null | undefined): boolean {
  return value === null || value === undefined;
}

/** The indices of a series' values that have no neighbour to be joined to by a line. */
const isolated: uPlot.Series.Points.Filter = (plot, seriesIndex) => {
  const values = plot.data[seriesIndex] ?? [];
  const alone: number[] = [];
  for (let i = 0; i < values.length; i++) {
    if (!isGap(values[i]) && isGap(values[i - 1]) && isGap(values[i + 1])) alone.push(i);
  }
  return alone;
};

function token(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/** A uPlot line chart over time. Everything it shows is passed in; it holds no data. */
export function TimeChart({
  xs,
  ys,
  lines,
  unit,
  yMax,
  reference = null,
  theme,
  height = 200,
}: TimeChartProps) {
  const holder = useRef<HTMLDivElement>(null);
  const plot = useRef<uPlot | null>(null);
  // Read by the draw hook and the scale, which outlive a render.
  const live = useRef({ yMax, reference });

  useEffect(() => {
    live.current = { yMax, reference };
  }, [yMax, reference]);

  const lineKey = lines.map((line) => `${line.slot}:${line.label}`).join("|");

  useEffect(() => {
    const element = holder.current;
    if (element === null) return;
    const ink = token("--text-muted");
    const grid = token("--grid");
    const font = `12px ${token("--font")}`;
    const axis = { stroke: ink, font, labelFont: font, grid: { stroke: grid, width: 1 } };
    const strokes = lineKey.split("|").map((key) => token(`--series-${key.split(":")[0] ?? "1"}`));

    const options: uPlot.Options = {
      width: Math.max(240, element.clientWidth),
      height,
      scales: {
        x: { time: false },
        y: { range: () => [0, live.current.yMax] },
      },
      axes: [
        {
          ...axis,
          label: "time before now (min:s)",
          ticks: { stroke: grid, width: 1 },
          values: (_plot, ticks) => ticks.map(formatAge),
        },
        {
          ...axis,
          label: unit,
          size: 64,
          ticks: { stroke: grid, width: 1 },
          values: (_plot, ticks) => ticks.map(formatValue),
        },
      ],
      series: [
        { label: "time", value: (_plot, value: number | null) => formatAge(value ?? 0) },
        ...lineKey.split("|").map((key): uPlot.Series => {
          const [slot = "1", label = ""] = key.split(":");
          return {
            label,
            stroke: token(`--series-${slot}`),
            width: 2,
            // A value with a gap on both sides has no line to be part of; without a dot
            // it would not be drawn at all.
            points: { show: true, size: 5, fill: token(`--series-${slot}`), filter: isolated },
            value: (_plot, value: number | null) =>
              value === null ? "–" : `${formatValue(value)} ${unit}`,
          };
        }),
      ],
      cursor: { y: false },
      // The key beside each name is a filled swatch of the line's colour.
      legend: { markers: { width: 0, fill: (_plot, index) => strokes[index - 1] ?? ink } },
      hooks: {
        draw: [
          (u) => {
            const line = live.current.reference;
            if (line === null) return;
            const y = u.valToPos(line.value, "y", true);
            const { left, top, width, height: plotHeight } = u.bbox;
            if (y < top || y > top + plotHeight) return;
            const context = u.ctx;
            context.save();
            context.strokeStyle = token("--text-secondary");
            context.fillStyle = token("--text-secondary");
            context.lineWidth = devicePixelRatio;
            context.beginPath();
            context.moveTo(left, y);
            context.lineTo(left + width, y);
            context.stroke();
            context.font = `${12 * devicePixelRatio}px ${token("--font")}`;
            context.textAlign = "right";
            context.textBaseline = "bottom";
            context.fillText(line.label, left + width - 4 * devicePixelRatio, y - 3);
            context.restore();
          },
        ],
      },
    };

    const created = new uPlot(options, [[]], element);
    plot.current = created;
    const resize = new ResizeObserver(() => {
      created.setSize({ width: Math.max(240, element.clientWidth), height });
    });
    resize.observe(element);
    return () => {
      resize.disconnect();
      created.destroy();
      plot.current = null;
    };
  }, [lineKey, unit, height, theme]);

  useEffect(() => {
    plot.current?.setData([xs, ...ys]);
  }, [xs, ys, yMax, reference, lineKey, unit, height, theme]);

  return <div ref={holder} className="chart" />;
}
