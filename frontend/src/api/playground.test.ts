import {
  IDLE_RUN,
  reduceRun,
  requestBody,
  streamGenerate,
  tokenGaps,
  ttft,
  type PlaygroundForm,
  type Run,
  type RunAction,
  type StreamedToken,
} from "./playground";
import { createSseParser } from "./sse";

const FORM: PlaygroundForm = {
  tenant: "acme",
  choose: "model",
  model: "premium",
  quality: "best",
  allowDowngrade: false,
  maxTokens: "32",
  prompt: "hello there",
};

const token = (index: number, t_ms: number): StreamedToken => ({
  index,
  token: `tok${index}`,
  t_ms,
});

const DONE = {
  id: "req-1",
  text: "[simulated output, 3 tokens]",
  model: "standard",
  usage: { prompt_tokens: 10, cached_tokens: 0, completion_tokens: 3, total_tokens: 13 },
  timing: { queue_ms: 0, ttft_ms: 9, total_ms: 17 },
  cost: { usd: 0.008 },
};

/** A fetch Response whose body arrives in the given pieces. */
function streamed(chunks: string[], headers: Record<string, string> = {}): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
  return new Response(body, { status: 200, headers });
}

function sse(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

async function run(response: Response | Error): Promise<{ actions: RunAction[]; final: Run }> {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => (response instanceof Error ? Promise.reject(response) : Promise.resolve(response))),
  );
  const actions: RunAction[] = [];
  await streamGenerate(
    requestBody(FORM),
    (action) => actions.push(action),
    new AbortController().signal,
  );
  return { actions, final: actions.reduce(reduceRun, IDLE_RUN) };
}

describe("createSseParser", () => {
  it("parses whole messages", () => {
    const parse = createSseParser();
    expect(parse('event: token\ndata: {"index":0}\n\nid: 7\nevent: event\ndata: x\n\n')).toEqual([
      { event: "token", data: '{"index":0}', id: null },
      { event: "event", data: "x", id: "7" },
    ]);
  });

  it("holds a message split across chunks until it is complete", () => {
    const parse = createSseParser();
    expect(parse("event: tok")).toEqual([]);
    expect(parse('en\ndata: {"a"')).toEqual([]);
    expect(parse(":1}\n")).toEqual([]);
    expect(parse("\nevent: done\n")).toEqual([{ event: "token", data: '{"a":1}', id: null }]);
    expect(parse("data: end\n\n")).toEqual([{ event: "done", data: "end", id: null }]);
  });

  it("skips comments, joins data lines and accepts CRLF", () => {
    const parse = createSseParser();
    expect(parse(": keep-alive\n\n")).toEqual([]);
    expect(parse("data: one\ndata: two\n\n")).toEqual([
      { event: "message", data: "one\ntwo", id: null },
    ]);
    expect(parse("event: a\r\ndata: b\r\n\r\n")).toEqual([{ event: "a", data: "b", id: null }]);
  });
});

describe("requestBody", () => {
  it("sends the model, the tenant and the limit, and always asks for a stream", () => {
    expect(requestBody(FORM)).toEqual({
      prompt: "hello there",
      stream: true,
      model: "premium",
      tenant_id: "acme",
      max_tokens: 32,
    });
  });

  it("sends a quality in place of a model, never both", () => {
    expect(requestBody({ ...FORM, choose: "quality" })).toEqual({
      prompt: "hello there",
      stream: true,
      quality: "best",
      tenant_id: "acme",
      max_tokens: 32,
    });
  });

  it("leaves out what was not set", () => {
    const body = requestBody({ ...FORM, tenant: "", maxTokens: "", allowDowngrade: true });
    expect(body).toEqual({
      prompt: "hello there",
      stream: true,
      model: "premium",
      allow_downgrade: true,
    });
  });
});

describe("timing", () => {
  it("takes TTFT from the first token", () => {
    expect(ttft([])).toBeNull();
    expect(ttft([token(0, 9), token(1, 13)])).toBe(9);
  });

  it("has one gap per token after the first", () => {
    expect(tokenGaps([])).toEqual([]);
    expect(tokenGaps([token(0, 9)])).toEqual([]);
    expect(tokenGaps([token(0, 9), token(1, 13), token(2, 17.5), token(3, 60)])).toEqual([
      4, 4.5, 42.5,
    ]);
  });
});

describe("streamGenerate", () => {
  it("posts the body and follows the stream token by token", async () => {
    const response = streamed(
      // Split in awkward places, as a network would.
      [
        sse("token", token(0, 9)) + "event: tok",
        `en\ndata: ${JSON.stringify(token(1, 13))}\n\n`,
        sse("token", token(2, 17)) + sse("done", DONE),
      ],
      { "X-Model-Used": "standard", "X-Downgraded-From": "premium" },
    );
    const { actions, final } = await run(response);

    expect(fetch).toHaveBeenCalledWith(
      "/v1/generate",
      expect.objectContaining({ method: "POST", body: JSON.stringify(requestBody(FORM)) }),
    );
    expect(actions.map((a) => a.kind)).toEqual([
      "sent",
      "headers",
      "token",
      "token",
      "token",
      "done",
    ]);
    expect(final.phase).toBe("done");
    // A streamed response has no X-Cost-USD header; the cost is in the final event.
    expect(final.headers).toEqual({
      modelUsed: "standard",
      downgradedFrom: "premium",
      costUsd: null,
    });
    expect(final.tokens.map((t) => t.t_ms)).toEqual([9, 13, 17]);
    expect(final.result?.cost.usd).toBe(0.008);
    expect(final.error).toBeNull();
  });

  it("reports a request the gateway refused, with its status and code", async () => {
    const refused = new Response(
      JSON.stringify({ error: { code: "queue_full", message: "Model 'premium' is overloaded." } }),
      { status: 429 },
    );
    const { final } = await run(refused);

    expect(final.phase).toBe("failed");
    expect(final.error).toEqual({
      status: 429,
      code: "queue_full",
      message: "Model 'premium' is overloaded.",
    });
    expect(final.tokens).toEqual([]);
  });

  it("reports an error that arrives after tokens, and keeps the tokens", async () => {
    const response = streamed([
      sse("token", token(0, 9)),
      sse("error", { code: "request_timeout", message: "It was cancelled." }),
    ]);
    const { final } = await run(response);

    expect(final.phase).toBe("failed");
    expect(final.error).toEqual({
      status: null,
      code: "request_timeout",
      message: "It was cancelled.",
    });
    expect(final.tokens).toHaveLength(1);
  });

  it("says so when the stream stops without finishing", async () => {
    const { final } = await run(streamed([sse("token", token(0, 9))]));

    expect(final.phase).toBe("failed");
    expect(final.error?.code).toBe("stream_closed");
  });

  it("reports a request that could not be sent", async () => {
    const { final } = await run(new TypeError("Failed to fetch"));

    expect(final.phase).toBe("failed");
    expect(final.error).toEqual({
      status: null,
      code: "network_error",
      message: "Failed to fetch",
    });
  });

  it("is cancelled, not failed, when the viewer aborts it", async () => {
    const controller = new AbortController();
    vi.stubGlobal(
      "fetch",
      vi.fn(() => {
        controller.abort();
        return Promise.reject(new DOMException("Aborted", "AbortError"));
      }),
    );
    const actions: RunAction[] = [];
    await streamGenerate(requestBody(FORM), (a) => actions.push(a), controller.signal);

    expect(actions.map((a) => a.kind)).toEqual(["sent", "cancelled"]);
  });

  it("starts each run from nothing", () => {
    const used = [{ kind: "token" as const, token: token(0, 9) }].reduce(reduceRun, IDLE_RUN);
    expect(reduceRun(used, { kind: "sent" })).toEqual({ ...IDLE_RUN, phase: "sending" });
  });
});
