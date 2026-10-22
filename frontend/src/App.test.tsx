import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "./App";
import type { TimeChartProps } from "./charts/TimeChart";
import { ThemeProvider } from "./hooks/useTheme";
import { FakeEventSource, installFakeEventSource } from "./test/fakeEventSource";
import type { ControlStatus } from "./api/control";
import { controlOff, controlSimulated, passthrough, simulated } from "./test/fixtures";

vi.mock("./charts/TimeChart", () => ({
  TimeChart: (props: TimeChartProps) => (
    <div data-testid="chart" data-unit={props.unit} data-reference={props.reference?.label ?? ""} />
  ),
}));

function open(snapshot: typeof simulated | null = simulated, control: ControlStatus = controlOff) {
  installFakeEventSource();
  vi.stubGlobal(
    "fetch",
    vi.fn((_url: string, init?: RequestInit) => {
      // The speed buttons post the speed they want and get the new status back.
      const asked = init?.body === undefined ? null : (JSON.parse(init.body as string) as object);
      const speed = asked !== null && "speed" in asked ? (asked.speed as number) : null;
      const answer =
        speed === null ? control : { ...control, simulation: { ...control.simulation, speed } };
      return Promise.resolve(new Response(JSON.stringify(answer)));
    }),
  );
  render(
    <ThemeProvider>
      <App />
    </ThemeProvider>,
  );
  if (snapshot !== null) FakeEventSource.latest.push("snapshot", snapshot);
  return userEvent.setup();
}

async function go(user: ReturnType<typeof userEvent.setup>, tab: string) {
  await user.click(screen.getByRole("tab", { name: tab }));
}

/** Each chart panel in a model's section, as [title, the reference line passed down]. */
function panels(section: string): [string, string][] {
  const region = screen.getByRole("region", { name: section });
  return within(region)
    .getAllByRole("region")
    .map((panel) => [
      within(panel).getByRole("heading").textContent,
      within(panel).queryByTestId("chart")?.dataset.reference ?? "no chart",
    ]);
}

describe("layout", () => {
  it("has the six tabs and opens on Overview", () => {
    open();

    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
      "Overview",
      "Latency",
      "Capacity",
      "Applications",
      "Events",
      "Playground",
    ]);
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getAllByRole("article").map((card) => card.getAttribute("aria-label"))).toEqual([
      "economy",
      "standard",
      "premium",
    ]);
  });

  it("switches tabs, keeps the choice in the URL, and moves with the arrow keys", async () => {
    const user = open();

    await go(user, "Applications");
    expect(window.location.hash).toBe("#/applications");
    expect(screen.getByRole("tab", { name: "Applications" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("tabpanel")).toHaveAttribute("aria-labelledby", "tab-applications");
    expect(screen.queryByRole("article")).not.toBeInTheDocument();

    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Events" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Events" })).toHaveFocus();
  });

  it("opens on the tab named in the URL", () => {
    window.location.hash = "#/playground";
    open();
    expect(screen.getByRole("heading", { name: "Send a request" })).toBeInTheDocument();
  });

  it("waits for the first snapshot", () => {
    open(null);
    expect(screen.getByText("Connecting…")).toBeInTheDocument();
    expect(screen.getByText("Waiting for the first snapshot…")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("connection", () => {
  it("shows a banner while disconnected and removes it when data flows again", () => {
    vi.useFakeTimers();
    try {
      open();
      expect(screen.getByText("Live")).toBeInTheDocument();
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();

      FakeEventSource.latest.fail();
      const banner = screen.getByRole("alert");
      expect(banner).toHaveTextContent("Disconnected from the gateway.");
      expect(banner).toHaveTextContent(/Showing data from .+\. Retrying…/);
      // The last data stays on screen under the banner.
      expect(screen.getAllByRole("article")).toHaveLength(3);

      act(() => {
        vi.advanceTimersByTime(1000);
      });
      FakeEventSource.latest.push("snapshot", { ...simulated, at_ms: simulated.at_ms + 2000 });
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(screen.getByText("Live")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("says so when nothing was ever received", () => {
    open(null);
    FakeEventSource.latest.fail();
    expect(screen.getByRole("alert")).toHaveTextContent("Nothing has been received yet.");
  });
});

describe("theme", () => {
  it("follows the system setting, then the viewer's choice, and remembers it", async () => {
    const user = open();
    expect(document.documentElement.dataset.theme).toBe("light");

    await user.click(screen.getByRole("button", { name: "Dark theme" }));
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(localStorage.getItem("gateway-dashboard-theme")).toBe("dark");

    await user.click(screen.getByRole("button", { name: "Light theme" }));
    expect(document.documentElement.dataset.theme).toBe("light");
  });

  it("starts dark when the system is dark, unless a choice was saved", () => {
    vi.stubGlobal("matchMedia", (query: string) => ({ matches: true, media: query }));
    open();
    expect(document.documentElement.dataset.theme).toBe("dark");
  });

  it("starts from the saved choice", () => {
    localStorage.setItem("gateway-dashboard-theme", "dark");
    open();
    expect(document.documentElement.dataset.theme).toBe("dark");
  });
});

describe("Latency tab", () => {
  it("has TTFT, ITL and total for every model, in ms, with the SLO on total only", async () => {
    const user = open();
    await go(user, "Latency");

    // Premium's SLO is 2000 ms and its latencies reach 3732.5 ms, so the line is drawn.
    expect(panels("premium latency")).toEqual([
      ["Time to first token (ms)", ""],
      ["Inter-token latency (ms)", ""],
      ["Total latency (ms)", "p99 SLO 2,000 ms"],
    ]);
    expect(screen.getAllByTestId("chart").every((chart) => chart.dataset.unit === "ms")).toBe(true);
    // Every chart can be paused.
    expect(screen.getAllByRole("button", { name: "Pause" })).toHaveLength(9);
  });

  it("says where the SLO is when it is far above the data", async () => {
    const user = open();
    await go(user, "Latency");

    // Economy's SLO is 5000 ms over latencies of about 50 ms.
    expect(panels("economy latency")[2]).toEqual(["Total latency (ms)", ""]);
    const economy = screen.getByRole("region", { name: "economy latency" });
    expect(
      within(economy).getByText(/p99 SLO 5,000 ms is above the range drawn\./),
    ).toBeInTheDocument();
  });

  it("says a model has no SLO instead of drawing one", async () => {
    const user = open(passthrough);
    await go(user, "Latency");

    const premium = screen.getByRole("region", { name: "premium latency" });
    expect(within(premium).getByText(/No SLO is set for this model\./)).toBeInTheDocument();
    expect(panels("premium latency")[2]).toEqual(["Total latency (ms)", ""]);
  });
});

describe("Capacity tab", () => {
  it("charts queue, batch, KV and cache, against their limits, with units", async () => {
    const user = open();
    await go(user, "Capacity");

    expect(panels("premium capacity")).toEqual([
      ["Queue depth (requests)", ""],
      ["Batch size (requests)", "max 8"],
      ["KV tokens used (tokens)", "capacity 16,384"],
      ["Prefix cache hit rate (%)", ""],
    ]);
    expect(screen.queryByText("Not available in this backend")).not.toBeInTheDocument();
  });

  it("shows every panel of a pass-through model as not available", async () => {
    const user = open(passthrough);
    await go(user, "Capacity");

    const premium = screen.getByRole("region", { name: "premium capacity" });
    expect(
      within(premium)
        .getAllByRole("note")
        .map((note) => note.textContent),
    ).toEqual([
      "Queue depthNot available in this backend",
      "Batch sizeNot available in this backend",
      "KV tokens usedNot available in this backend",
      "Prefix cache hit rateNot available in this backend",
    ]);
    expect(screen.queryByTestId("chart")).not.toBeInTheDocument();
  });
});

describe("Applications tab", () => {
  it("lists each application's plan, spend, budget use, rejections and downgrades", async () => {
    const user = open();
    await go(user, "Applications");

    const row = screen.getByRole("row", { name: /^acme/ });
    const cells = within(row)
      .getAllByRole("cell")
      .map((cell) => cell.textContent);
    expect(cells).toEqual([
      "enterprise",
      "$0.000927",
      "691 of 100,000,000 tokens (0.0%)",
      "14",
      "0",
      "2",
      "0", // rejected by the rate limit
      "0",
      "1",
    ]);
    expect(within(row).getByRole("meter", { name: "acme token budget used" })).toHaveAttribute(
      "aria-valuenow",
      "0",
    );
    expect(screen.getAllByRole("rowheader").map((cell) => cell.textContent)).toEqual([
      "initech",
      "globex",
      "acme",
    ]);
  });

  it("flags an application that has used up its budget", async () => {
    const applications = simulated.applications.applications ?? {};
    const initech = applications.initech;
    if (initech === undefined) throw new Error("fixture has no application initech");
    const user = open({
      ...simulated,
      applications: {
        available: true,
        applications: {
          ...applications,
          initech: { ...initech, used_tokens: 100_012, remaining_tokens: 0 },
        },
      },
    });
    await go(user, "Applications");

    const row = screen.getByRole("row", { name: /^initech/ });
    expect(within(row).getByText("Budget used up")).toBeInTheDocument();
    expect(within(row).getByRole("meter")).toHaveClass("meter-exhausted");
    expect(within(row).getByRole("meter")).toHaveAttribute("aria-valuenow", "100");
  });

  it("says applications are not available when the gateway has no plans", async () => {
    const user = open(passthrough);
    await go(user, "Applications");

    expect(screen.getByRole("note")).toHaveTextContent("Not available in this backend");
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });
});

describe("Events tab", () => {
  function feed(): string[] {
    return within(screen.getByRole("list", { name: "Events" }))
      .getAllByRole("listitem")
      .map((item) => item.textContent);
  }

  it("lists events newest first, with what happened and when", async () => {
    const user = open();
    await go(user, "Events");

    expect(feed()).toEqual([
      "DowngradeA request for premium was served by standard.acme · 0 s ago · #4",
      "State changepremium is degraded: recent p99 2,910 ms is over its SLO.1 s ago · #3",
      "Queue fullpremium refused a request: 2 waiting, limit 2.acme · 4 s ago · #2",
      "Queue fullpremium refused a request: 2 waiting, limit 2.acme · 4 s ago · #1",
    ]);
    expect(screen.getByText(/Showing 4 of 4 events held/)).toBeInTheDocument();
  });

  it("filters by type and application", async () => {
    const user = open();
    await go(user, "Events");

    await user.selectOptions(screen.getByLabelText("Type"), "queue_full");
    expect(feed()).toHaveLength(2);
    expect(screen.getByText(/Showing 2 of 4 events held/)).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Type"), "state_change");
    await user.selectOptions(screen.getByLabelText("Application"), "acme");
    expect(screen.getByText("No events match.")).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Type"), "");
    expect(feed()).toHaveLength(3);
  });

  it("shows new events as they arrive, unless paused", async () => {
    const user = open();
    await go(user, "Events");
    const timeout = {
      seq: 5,
      at_ms: 6800,
      type: "timeout",
      model: "economy",
      application_id: null,
      data: { timeout_ms: 120_000 },
    };

    FakeEventSource.latest.push("event", timeout);
    expect(feed()[0]).toContain("A request to economy was cancelled after 120,000 ms.");

    await user.click(screen.getByRole("button", { name: "Pause" }));
    FakeEventSource.latest.push("event", { ...timeout, seq: 6 });
    expect(feed()).toHaveLength(5);
    expect(screen.getByText(/Paused: new events are not shown\./)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Resume" }));
    expect(feed()).toHaveLength(6);
  });

  it("says how many events the gateway had already dropped", async () => {
    const user = open();
    FakeEventSource.latest.push("events_missed", { missed: 12 });
    await go(user, "Events");

    expect(screen.getByText(/12 older events had already left the log/)).toBeInTheDocument();
  });
});

describe("simulated live mode", () => {
  it("is labelled on every tab, with the seed, the load and the speed", async () => {
    const user = open(simulated, controlSimulated);

    const banner = await screen.findByRole("status", { name: "Simulated live mode" });
    expect(banner).toHaveTextContent("Simulated live mode. No model is running.");
    expect(banner).toHaveTextContent("simulated, not wall-clock");
    expect(banner).toHaveTextContent("Seed 7 · load preset “steady”");
    expect(screen.getByText("Simulated")).toBeInTheDocument();
    expect(within(banner).getByRole("button", { name: "5×" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    for (const tab of ["Latency", "Capacity", "Applications", "Events", "Playground"]) {
      await go(user, tab);
      expect(screen.getByRole("status", { name: "Simulated live mode" })).toBeInTheDocument();
    }
  });

  it("changes the replay speed", async () => {
    const user = open(simulated, controlSimulated);
    const banner = await screen.findByRole("status", { name: "Simulated live mode" });

    await user.click(within(banner).getByRole("button", { name: "20×" }));

    expect(fetch).toHaveBeenCalledWith(
      "/v1/control/speed",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ speed: 20 }) }),
    );
    expect(within(banner).getByRole("button", { name: "20×" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(within(banner).getByRole("button", { name: "5×" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("is not shown, and nothing is called simulated, on an ordinary gateway", async () => {
    open();
    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith("/v1/control", expect.anything());
    });

    expect(screen.queryByRole("status", { name: "Simulated live mode" })).not.toBeInTheDocument();
    expect(screen.queryByText("Simulated")).not.toBeInTheDocument();
  });
});
