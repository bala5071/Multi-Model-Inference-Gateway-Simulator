import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ChartPanel, type ChartPanelProps } from "./ChartPanel";
import type { TimeChartProps } from "./TimeChart";

// uPlot draws on a canvas, which jsdom does not have. The stand-in shows what the panel
// passed down, which is the panel's whole job.
vi.mock("./TimeChart", () => ({
  TimeChart: (props: TimeChartProps) => (
    <div
      data-testid="chart"
      data-xs={JSON.stringify(props.xs)}
      data-ys={JSON.stringify(props.ys)}
      data-unit={props.unit}
      data-ymax={props.yMax}
      data-reference={props.reference?.label ?? ""}
    />
  ),
}));

const BASE: ChartPanelProps = {
  title: "Total latency",
  unit: "ms",
  columns: { xs: [1000, 2000, 3000], ys: [[10, 20, 30]] },
  lines: [{ label: "p99", slot: 2, reducer: "max" }],
  nowMs: 3500,
  stepMs: 1000,
};

function chart() {
  const element = screen.getByTestId("chart");
  return {
    xs: JSON.parse(element.dataset.xs ?? "[]") as number[],
    ys: JSON.parse(element.dataset.ys ?? "[]") as (number | null)[][],
    yMax: Number(element.dataset.ymax),
    reference: element.dataset.reference,
    unit: element.dataset.unit,
  };
}

describe("ChartPanel", () => {
  it("labels the chart with its title and units", () => {
    render(<ChartPanel {...BASE} />);

    expect(screen.getByRole("heading", { name: "Total latency (ms)" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Total latency (ms)" })).toBeInTheDocument();
    expect(chart().unit).toBe("ms");
    expect(chart().xs).toEqual([-2.5, -1.5, -0.5]);
    expect(chart().ys).toEqual([[10, 20, 30]]);
  });

  it("holds the data shown while paused and catches up when resumed", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<ChartPanel {...BASE} />);

    await user.click(screen.getByRole("button", { name: "Pause" }));
    expect(screen.getByRole("button", { name: "Resume" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("Paused. New data is not shown.")).toBeInTheDocument();

    const newer = { xs: [1000, 2000, 3000, 4000], ys: [[10, 20, 30, 999]] };
    rerender(<ChartPanel {...BASE} columns={newer} nowMs={4500} />);
    // Neither the new point nor the new "now" has reached the chart.
    expect(chart().xs).toEqual([-2.5, -1.5, -0.5]);
    expect(chart().ys).toEqual([[10, 20, 30]]);

    await user.click(screen.getByRole("button", { name: "Resume" }));
    expect(chart().xs).toEqual([-3.5, -2.5, -1.5, -0.5]);
    expect(chart().ys).toEqual([[10, 20, 30, 999]]);
    expect(screen.queryByText("Paused. New data is not shown.")).not.toBeInTheDocument();
  });

  it("downsamples to the point limit and says what a point covers", () => {
    const xs = Array.from({ length: 900 }, (_, i) => i * 1000);
    const ys = [xs.map((_, i) => (i === 500 ? 7000 : 10))];
    render(<ChartPanel {...BASE} columns={{ xs, ys }} nowMs={900_000} />);

    expect(chart().xs).toHaveLength(300);
    // The one spike is still there.
    expect(Math.max(...(chart().ys[0] as number[]))).toBe(7000);
    expect(screen.getByText(/Each point covers 3 s\./)).toBeInTheDocument();
  });

  it("does not mention downsampling when every point is drawn", () => {
    render(<ChartPanel {...BASE} />);
    expect(screen.queryByText(/Each point covers/)).not.toBeInTheDocument();
  });

  it("draws a reference line that is near the data", () => {
    render(<ChartPanel {...BASE} reference={{ label: "p99 SLO 50 ms", value: 50 }} />);

    expect(chart().reference).toBe("p99 SLO 50 ms");
    expect(chart().yMax).toBeCloseTo(55);
    expect(screen.queryByText(/above the range drawn/)).not.toBeInTheDocument();
  });

  it("says where a reference line is when it is too far above the data to draw", () => {
    render(<ChartPanel {...BASE} reference={{ label: "p99 SLO 30,000 ms", value: 30_000 }} />);

    expect(chart().reference).toBe("");
    expect(chart().yMax).toBeCloseTo(33);
    expect(screen.getByText(/p99 SLO 30,000 ms is above the range drawn\./)).toBeInTheDocument();
  });

  it("runs the axis to a fixed maximum", () => {
    render(<ChartPanel {...BASE} fixedMax={100} />);
    expect(chart().yMax).toBe(105);
  });

  it("says so when there is nothing to draw", () => {
    render(<ChartPanel {...BASE} columns={{ xs: [1000, 2000], ys: [[null, null]] }} />);

    expect(screen.getByText("No data in this window yet.")).toBeInTheDocument();
    expect(screen.queryByTestId("chart")).not.toBeInTheDocument();
  });

  it("offers the same numbers as a table, newest first", async () => {
    const user = userEvent.setup();
    render(<ChartPanel {...BASE} />);
    expect(screen.queryByRole("table")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Table" }));

    const table = screen.getByRole("table", { name: /Total latency, newest 20 points, in ms/ });
    const rows = within(table)
      .getAllByRole("row")
      .map((row) => [...row.querySelectorAll("th,td")].map((cell) => cell.textContent));
    expect(rows).toEqual([
      ["Time before now", "p99"],
      ["-0:01", "30"],
      ["-0:02", "20"],
      ["-0:03", "10"],
    ]);
  });
});
