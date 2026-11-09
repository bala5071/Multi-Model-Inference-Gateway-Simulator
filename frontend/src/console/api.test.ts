import { ApiError, CSRF_HEADER, makeApi, probeWrite } from "./api";

interface Call {
  path: string;
  init: RequestInit;
}

function stub(answers: { status: number; body: unknown }[]): {
  fetcher: typeof fetch;
  calls: Call[];
} {
  const calls: Call[] = [];
  const fetcher = (path: string, init?: RequestInit) => {
    calls.push({ path, init: init ?? {} });
    const answer = answers.shift() ?? { status: 500, body: null };
    return Promise.resolve(
      new Response(JSON.stringify(answer.body), {
        status: answer.status,
        headers: { "Content-Type": "application/json" },
      }),
    );
  };
  return { fetcher: fetcher as unknown as typeof fetch, calls };
}

function headers(call: Call | undefined): Record<string, string> {
  return (call?.init.headers ?? {}) as Record<string, string>;
}

describe("makeApi", () => {
  it("sends the CSRF token on every change, read from memory at each call", async () => {
    let token: string | null = "token-one";
    const { fetcher, calls } = stub([
      { status: 200, body: {} },
      { status: 200, body: {} },
      { status: 200, body: {} },
    ]);
    const api = makeApi(() => token, fetcher);
    await api.send("PUT", "/v1/x", { a: 1 });
    token = "token-two";
    await api.send("POST", "/v1/y");
    await api.get("/v1/z");
    expect(headers(calls[0])[CSRF_HEADER]).toBe("token-one");
    expect(headers(calls[1])[CSRF_HEADER]).toBe("token-two");
    // A read changes nothing and carries no token.
    expect(headers(calls[2])[CSRF_HEADER]).toBeUndefined();
    expect(calls[0]?.init.body).toBe('{"a":1}');
  });

  it("asks for nothing to be cached and sends the cookie only to its own origin", async () => {
    const { fetcher, calls } = stub([{ status: 200, body: {} }]);
    await makeApi(() => null, fetcher).get("/v1/auth/me");
    expect(calls[0]?.init.credentials).toBe("same-origin");
    expect(calls[0]?.init.cache).toBe("no-store");
  });

  it("turns a refusal into an ApiError with the gateway's code and message", async () => {
    const { fetcher } = stub([
      {
        status: 403,
        body: { error: { code: "forbidden", message: "Not permitted, or not found." } },
      },
    ]);
    const failure = await makeApi(() => null, fetcher)
      .get("/v1/admin/orgs/x/settings")
      .catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(ApiError);
    expect(failure).toMatchObject({ status: 403, code: "forbidden" });
  });

  it("names an answer that is not the gateway's error shape by its status", async () => {
    const { fetcher } = stub([{ status: 502, body: "bad gateway" }]);
    await expect(makeApi(() => null, fetcher).get("/v1/x")).rejects.toMatchObject({
      status: 502,
      code: "unexpected_answer",
    });
  });
});

describe("probeWrite", () => {
  it("dry-runs an empty change against the gateway, which writes nothing", async () => {
    const { fetcher, calls } = stub([{ status: 200, body: { valid: true } }]);
    expect(
      await probeWrite(
        makeApi(() => "t", fetcher),
        "/v1/admin/orgs/o/settings",
        4,
      ),
    ).toBe("allowed");
    expect(calls[0]?.path).toBe("/v1/admin/orgs/o/settings/validate");
    expect(calls[0]?.init.method).toBe("POST");
    expect(JSON.parse(calls[0]?.init.body as string)).toEqual({
      expected_version: 4,
      set: {},
      unset: [],
    });
  });

  it("is forbidden on the gateway's 403", async () => {
    const { fetcher } = stub([
      {
        status: 403,
        body: { error: { code: "forbidden", message: "Not permitted, or not found." } },
      },
    ]);
    expect(
      await probeWrite(
        makeApi(() => "t", fetcher),
        "/b",
        1,
      ),
    ).toBe("forbidden");
  });

  it.each([
    [401, "session_required"],
    [403, "csrf_failed"],
    [503, "state_unavailable"],
    [500, "internal"],
  ])("is unknown, never allowed, on a %i %s", async (status, code) => {
    const { fetcher } = stub([{ status, body: { error: { code, message: "no" } } }]);
    expect(
      await probeWrite(
        makeApi(() => "t", fetcher),
        "/b",
        1,
      ),
    ).toBe("unknown");
  });
});
