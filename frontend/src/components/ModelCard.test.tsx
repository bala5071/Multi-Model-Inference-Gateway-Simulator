import { render, screen, within } from "@testing-library/react";
import type { ModelSnapshot } from "../api/types";
import { passthrough, simulated } from "../test/fixtures";
import { ModelCard } from "./ModelCard";

function card(snapshot: typeof simulated, name: string, change?: Partial<ModelSnapshot>) {
  const model = snapshot.models[name];
  if (model === undefined) throw new Error(`fixture has no model ${name}`);
  const shown = { ...model, ...change };
  render(<ModelCard name={name} model={shown} points={shown.series} />);
  return within(screen.getByRole("article", { name }));
}

/** The text of the stat whose label is `label`. */
function stat(view: { getByText: (text: string) => HTMLElement }, label: string): string {
  return view.getByText(label).parentElement?.textContent ?? "";
}

describe("ModelCard", () => {
  it("shows a degraded model with its latency over the SLO", () => {
    const view = card(simulated, "premium");

    expect(view.getByText("Degraded")).toBeInTheDocument();
    expect(stat(view, "p99 total latency")).toContain("3,733 ms");
    expect(view.getByText("SLO 2,000 ms")).toBeInTheDocument();
    const meter = view.getByRole("meter", { name: "premium p99 total latency against its SLO" });
    // 3732.5 ms against 2000 ms: the bar is full, and marked as over.
    expect(meter).toHaveAttribute("aria-valuenow", "100");
    expect(meter).toHaveClass("meter-exhausted");
    expect(stat(view, "p99 TTFT")).toContain("2,150 ms");
  });

  it("shows a healthy model with its load, queue and batch", () => {
    const view = card(simulated, "economy");

    expect(view.getByText("Healthy")).toBeInTheDocument();
    // The last second that ended had two arrivals.
    expect(stat(view, "Requests")).toContain("2 req/s");
    expect(stat(view, "In flight")).toContain("0");
    expect(stat(view, "Queue depth")).toContain("0 waiting");
    expect(stat(view, "Batch occupancy")).toContain("0 of 32 seats");
    expect(view.getByRole("meter", { name: "economy batch occupancy" })).toHaveAttribute(
      "aria-valuenow",
      "0",
    );
    expect(view.queryByText("Not available in this backend")).not.toBeInTheDocument();
  });

  it("fills the batch meter by seats taken", () => {
    const view = card(simulated, "premium", {
      batch: { available: true, size: 6, max_size: 8 },
      queue: { available: true, length: 3 },
    });

    expect(stat(view, "Batch occupancy")).toContain("6 of 8 seats");
    expect(view.getByRole("meter", { name: "premium batch occupancy" })).toHaveAttribute(
      "aria-valuenow",
      "75",
    );
    expect(stat(view, "Queue depth")).toContain("3 waiting");
  });

  it("says a pass-through model's queue and batch are not available, and shows no zero", () => {
    const view = card(passthrough, "premium");

    const notes = view.getAllByRole("note");
    expect(notes.map((note) => note.textContent)).toEqual([
      "Queue depthNot available in this backend",
      "Batch occupancyNot available in this backend",
    ]);
    expect(view.queryByText(/waiting/)).not.toBeInTheDocument();
    expect(view.queryByText(/seats/)).not.toBeInTheDocument();
    expect(view.queryByRole("meter")).not.toBeInTheDocument();
    // What the gateway measures itself is still shown.
    expect(stat(view, "p99 total latency")).toContain("70 ms");
    expect(stat(view, "p99 TTFT")).toContain("50 ms");
  });

  it("marks a model with no SLO as such, not as healthy", () => {
    const view = card(passthrough, "premium");

    expect(view.getByText("No SLO")).toBeInTheDocument();
    expect(view.queryByText("Healthy")).not.toBeInTheDocument();
    expect(view.getByText("No SLO for this model")).toBeInTheDocument();
  });

  it("shows a dash where there is nothing to measure yet", () => {
    // Economy was never used in the pass-through fixture, and no second has ended.
    const view = card(passthrough, "economy");

    expect(stat(view, "Requests")).toContain("– req/s");
    expect(stat(view, "p99 total latency")).toContain("– ms");
  });
});
