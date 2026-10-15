import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ControlStatus } from "../api/control";
import { EMPTY_STATE, reduce } from "../api/store";
import type { Control } from "../hooks/useControl";
import { controlOff, controlSimulated, passthrough, simulated } from "../test/fixtures";
import { Playground } from "./Playground";

const DONE = {
  id: "req-1",
  text: "[simulated output, 4 tokens]",
  model: "standard",
  usage: { prompt_tokens: 9, cached_tokens: 2, completion_tokens: 4, total_tokens: 13 },
  timing: { queue_ms: 1.5, ttft_ms: 9, total_ms: 60 },
  cost: { usd: 0.0000552 },
};

function sse(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

/** Stub fetch with a stream of four tokens at 9, 13, 17.5 and 60 ms, then the result. */
function stubStream(headers: Record<string, string>) {
  const encoder = new TextEncoder();
  const times = [9, 13, 17.5, 60];
  const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(() => {
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        times.forEach((t_ms, index) => {
          controller.enqueue(encoder.encode(sse("token", { index, token: `tok${index}`, t_ms })));
        });
        controller.enqueue(encoder.encode(sse("done", DONE)));
        controller.close();
      },
    });
    return Promise.resolve(new Response(body, { status: 200, headers }));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function control(status: ControlStatus | null, error: string | null = null) {
  const actions = {
    start: vi.fn(() => Promise.resolve()),
    stop: vi.fn(() => Promise.resolve()),
    speed: vi.fn(() => Promise.resolve()),
  };
  const value: Control = { status, error, ...actions };
  return { value, ...actions };
}

function open(snapshot = simulated, status: ControlStatus | null = controlOff) {
  const state = reduce(EMPTY_STATE, { kind: "snapshot", snapshot });
  const made = control(status);
  render(<Playground state={state} control={made.value} />);
  return { user: userEvent.setup(), ...made };
}

function sentBody(fetchMock: ReturnType<typeof stubStream>): unknown {
  const init = fetchMock.mock.calls[0]?.[1];
  return JSON.parse(typeof init?.body === "string" ? init.body : "null");
}

describe("request form", () => {
  it("offers the gateway's tenants and models, and sends what was chosen", async () => {
    const fetchMock = stubStream({ "X-Model-Used": "premium" });
    const { user } = open();

    expect(
      within(screen.getByLabelText("Tenant"))
        .getAllByRole("option")
        .map((o) => o.textContent),
    ).toEqual(["initech (free)", "globex (pro)", "acme (enterprise)"]);
    await user.selectOptions(screen.getByLabelText("Tenant"), "acme");
    await user.selectOptions(screen.getByLabelText("Model"), "premium");
    await user.clear(screen.getByLabelText("Max tokens"));
    await user.type(screen.getByLabelText("Max tokens"), "4");
    await user.click(screen.getByLabelText("Allow downgrade"));
    await user.clear(screen.getByLabelText("Prompt"));
    await user.type(screen.getByLabelText("Prompt"), "hello");
    await user.click(screen.getByRole("button", { name: "Send" }));

    await screen.findByText("Finished.");
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/v1/generate");
    expect(sentBody(fetchMock)).toEqual({
      prompt: "hello",
      stream: true,
      model: "premium",
      tenant_id: "acme",
      allow_downgrade: true,
      max_tokens: 4,
    });
  });

  it("sends a quality in place of a model", async () => {
    const fetchMock = stubStream({});
    const { user } = open();

    await user.selectOptions(screen.getByLabelText("Choose by"), "quality");
    expect(screen.queryByLabelText("Model")).not.toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Quality"), "best");
    await user.click(screen.getByRole("button", { name: "Send" }));

    await screen.findByText("Finished.");
    const body = sentBody(fetchMock) as Record<string, unknown>;
    expect(body.quality).toBe("best");
    expect(body).not.toHaveProperty("model");
  });

  it("has no tenant to choose on a gateway without plans, and sends none", async () => {
    const fetchMock = stubStream({});
    const { user } = open(passthrough);

    expect(screen.queryByLabelText("Tenant")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Send" }));

    await screen.findByText("Finished.");
    expect(sentBody(fetchMock)).not.toHaveProperty("tenant_id");
  });

  it("cannot send an empty prompt", async () => {
    const { user } = open();
    await user.clear(screen.getByLabelText("Prompt"));
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
  });
});

describe("response", () => {
  it("shows the tokens, TTFT, the gap before each token, and the headers", async () => {
    stubStream({ "X-Model-Used": "standard", "X-Downgraded-From": "premium" });
    const { user } = open();
    expect(screen.getByText("Nothing sent yet.")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Send" }));
    await screen.findByText("Finished.");

    expect(screen.getByLabelText("Tokens")).toHaveTextContent("tok0 tok1 tok2 tok3");
    const fact = (label: string) => screen.getByText(label).parentElement?.textContent;
    expect(fact("Time to first token")).toBe("Time to first token9 ms");
    expect(fact("Tokens")).toBe("Tokens4");
    expect(fact("Total")).toBe("Total60 ms");
    expect(fact("Queue wait")).toBe("Queue wait1.5 ms");
    expect(fact("Prompt tokens")).toBe("Prompt tokens9 (2 cached)");

    // Tokens at 9, 13, 17.5 and 60 ms: gaps of 4, 4.5 and 42.5 ms, in ms.
    const bars = within(screen.getByRole("list", { name: "Inter-token gaps in ms" }));
    expect(bars.getAllByRole("listitem").map((bar) => bar.getAttribute("aria-label"))).toEqual([
      "token 1: 4 ms",
      "token 2: 4.5 ms",
      "token 3: 42.5 ms",
    ]);
    expect(screen.getByText(/3 gaps, the tallest bar is 42.5 ms/)).toBeInTheDocument();

    const headers = within(screen.getByRole("table", { name: "Response headers" }));
    expect(headers.getAllByRole("row").map((row) => row.textContent)).toEqual([
      "X-Model-Usedstandard",
      "X-Downgraded-Frompremium",
      // Not a header on a streamed response; the dashboard says where the cost came from.
      "X-Cost-USDnot sent on a streamed response; the final event gives $0.0000552000",
    ]);
  });

  it("says a header was not sent, not that it was empty", async () => {
    stubStream({ "X-Model-Used": "premium" });
    const { user } = open();

    await user.click(screen.getByRole("button", { name: "Send" }));
    await screen.findByText("Finished.");

    expect(screen.getByText("not sent: the request was not downgraded")).toBeInTheDocument();
  });

  it("shows why a request was refused", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(
            JSON.stringify({
              error: { code: "model_not_in_plan", message: "Tenant 'initech' is on 'free'." },
            }),
            { status: 403 },
          ),
        ),
      ),
    );
    const { user } = open();

    await user.click(screen.getByRole("button", { name: "Send" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("HTTP 403 model_not_in_plan: Tenant 'initech' is on 'free'.");
    expect(screen.getByText("Failed.")).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Response headers" })).not.toBeInTheDocument();
  });
});

describe("load control", () => {
  it("is not available unless the gateway enables it", () => {
    open(simulated, controlOff);

    const section = within(screen.getByRole("region", { name: "Load" }));
    expect(section.getByRole("note")).toHaveTextContent(
      "Load controlNot available in this backend",
    );
    expect(section.getByText(/GATEWAY_LOAD_CONTROL=1/)).toBeInTheDocument();
    expect(section.queryByRole("button", { name: "Start" })).not.toBeInTheDocument();
  });

  it("shows the running preset, its seed and its counts", () => {
    open(simulated, controlSimulated);

    const section = within(screen.getByRole("region", { name: "Load" }));
    expect(section.getByRole("status", { name: "Load status" })).toHaveTextContent(
      "Running “steady” with seed 7.",
    );
    expect(section.getByLabelText("Seed")).toHaveValue(7);
    expect(section.getByLabelText("Preset")).toHaveValue("steady");
    expect(
      within(section.getByLabelText("Preset"))
        .getAllByRole("option")
        .map((o) => o.textContent),
    ).toEqual(["steady", "burst", "overload", "long-prompts"]);
    const fact = (label: string) => section.getByText(label).parentElement?.textContent;
    expect(fact("Sent")).toBe("Sent87");
    expect(fact("Served")).toBe("Served74");
    expect(fact("Refused")).toBe("Refused0");
    expect(fact("In progress")).toBe("In progress13");
  });

  it("starts the chosen preset with the seed typed, and stops it", async () => {
    const { user, start, stop } = open(simulated, controlSimulated);
    const section = within(screen.getByRole("region", { name: "Load" }));

    await user.selectOptions(section.getByLabelText("Preset"), "overload");
    expect(section.getByText(/Mostly premium, at several times/)).toBeInTheDocument();
    await user.clear(section.getByLabelText("Seed"));
    await user.type(section.getByLabelText("Seed"), "42");
    await user.click(section.getByRole("button", { name: "Restart" }));
    expect(start).toHaveBeenCalledWith("overload", 42);

    await user.click(section.getByRole("button", { name: "Stop" }));
    expect(stop).toHaveBeenCalledTimes(1);
  });

  it("offers Start, with the gateway's seed, when nothing is running", () => {
    const load = controlSimulated.load;
    const idle: ControlStatus = {
      ...controlSimulated,
      seed: 21,
      load: {
        ...load,
        status: load.status && { ...load.status, running: false, preset: null, seed: null },
      },
    };
    open(simulated, idle);

    const section = within(screen.getByRole("region", { name: "Load" }));
    expect(section.getByRole("button", { name: "Start" })).toBeEnabled();
    expect(section.getByRole("button", { name: "Stop" })).toBeDisabled();
    expect(section.getByLabelText("Seed")).toHaveValue(21);
    expect(section.getByRole("status", { name: "Load status" })).toHaveTextContent(
      "No load has been started.",
    );
  });

  it("shows what went wrong with the last action", async () => {
    const state = reduce(EMPTY_STATE, { kind: "snapshot", snapshot: simulated });
    const made = control(controlSimulated, "Unknown load preset 'tsunami'.");
    render(<Playground state={state} control={made.value} />);

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent("Unknown load preset 'tsunami'.");
    });
  });
});
