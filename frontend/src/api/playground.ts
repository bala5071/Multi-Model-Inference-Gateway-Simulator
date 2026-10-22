// One streamed request to /v1/generate, as the playground follows it.

import { createSseParser } from "./sse";

export const QUALITIES = ["cheapest", "balanced", "best"] as const;
export type Quality = (typeof QUALITIES)[number];

export interface PlaygroundForm {
  /** The application to send as; "" when the gateway does not offer the Playground. */
  application: string;
  choose: "model" | "quality";
  model: string;
  quality: Quality;
  allowDowngrade: boolean;
  /** As typed; empty means no limit. */
  maxTokens: string;
  prompt: string;
}

/** Where the Playground sends: its own endpoint when the gateway offers it. */
export function requestPath(playgroundAvailable: boolean): string {
  return playgroundAvailable ? "/v1/playground/generate" : "/v1/generate";
}

/** The JSON body for a form. Exactly one of model and quality is sent. */
export function requestBody(form: PlaygroundForm): Record<string, unknown> {
  const body: Record<string, unknown> = { prompt: form.prompt, stream: true };
  if (form.choose === "model") body.model = form.model;
  else body.quality = form.quality;
  if (form.application !== "") body.application_id = form.application;
  if (form.allowDowngrade) body.allow_downgrade = true;
  const maxTokens = Number.parseInt(form.maxTokens, 10);
  if (Number.isFinite(maxTokens)) body.max_tokens = maxTokens;
  return body;
}

export interface StreamedToken {
  index: number;
  token: string;
  /** Milliseconds after the request arrived, on the gateway's clock. */
  t_ms: number;
}

/** The final event of a successful stream: the same body a non-streaming request gets. */
export interface GenerateResult {
  id: string;
  text: string;
  model: string;
  usage: {
    prompt_tokens: number;
    cached_tokens: number;
    completion_tokens: number;
    total_tokens: number;
  };
  timing: { queue_ms: number; ttft_ms: number; total_ms: number };
  cost: { usd: number };
}

export interface RunError {
  /** The HTTP status when the request was refused outright; null if the stream broke. */
  status: number | null;
  code: string;
  message: string;
}

export interface Run {
  phase: "idle" | "sending" | "streaming" | "done" | "failed" | "cancelled";
  /** Present once the response's headers have arrived. null means the header was not sent. */
  headers: { modelUsed: string | null; downgradedFrom: string | null; costUsd: string | null };
  tokens: StreamedToken[];
  result: GenerateResult | null;
  error: RunError | null;
}

export const IDLE_RUN: Run = {
  phase: "idle",
  headers: { modelUsed: null, downgradedFrom: null, costUsd: null },
  tokens: [],
  result: null,
  error: null,
};

export type RunAction =
  | { kind: "sent" }
  | { kind: "headers"; headers: Run["headers"] }
  | { kind: "token"; token: StreamedToken }
  | { kind: "done"; result: GenerateResult }
  | { kind: "error"; error: RunError }
  | { kind: "cancelled" };

export function reduceRun(run: Run, action: RunAction): Run {
  switch (action.kind) {
    case "sent":
      return { ...IDLE_RUN, phase: "sending" };
    case "headers":
      return { ...run, phase: "streaming", headers: action.headers };
    case "token":
      return { ...run, tokens: [...run.tokens, action.token] };
    case "done":
      return { ...run, phase: "done", result: action.result };
    case "error":
      return { ...run, phase: "failed", error: action.error };
    case "cancelled":
      return { ...run, phase: "cancelled" };
  }
}

/** Time to the first token, in ms after arrival; null before one has come. */
export function ttft(tokens: readonly StreamedToken[]): number | null {
  return tokens[0]?.t_ms ?? null;
}

/** The gap before each token after the first, in ms: one fewer than there are tokens. */
export function tokenGaps(tokens: readonly StreamedToken[]): number[] {
  const gaps: number[] = [];
  for (let i = 1; i < tokens.length; i++) {
    const earlier = tokens[i - 1];
    const later = tokens[i];
    if (earlier !== undefined && later !== undefined) gaps.push(later.t_ms - earlier.t_ms);
  }
  return gaps;
}

interface ErrorBody {
  error?: { code?: string; message?: string };
}

/**
 * Send the request and report what happens through `dispatch`, token by token.
 * Resolves when the stream has ended, however it ended. Never rejects.
 */
export async function streamGenerate(
  body: Record<string, unknown>,
  dispatch: (action: RunAction) => void,
  signal: AbortSignal,
  path = "/v1/generate",
): Promise<void> {
  dispatch({ kind: "sent" });
  try {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    });
    if (!response.ok || response.body === null) {
      // Refused before any token: an ordinary JSON error.
      const refused = (await response.json().catch(() => ({}))) as ErrorBody;
      dispatch({
        kind: "error",
        error: {
          status: response.status,
          code: refused.error?.code ?? "http_error",
          message: refused.error?.message ?? `The gateway answered HTTP ${response.status}.`,
        },
      });
      return;
    }
    dispatch({
      kind: "headers",
      headers: {
        modelUsed: response.headers.get("X-Model-Used"),
        downgradedFrom: response.headers.get("X-Downgraded-From"),
        costUsd: response.headers.get("X-Cost-USD"),
      },
    });

    const parse = createSseParser();
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let finished = false;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      for (const message of parse(decoder.decode(value, { stream: true }))) {
        if (message.event === "token") {
          dispatch({ kind: "token", token: JSON.parse(message.data) as StreamedToken });
        } else if (message.event === "done") {
          finished = true;
          dispatch({ kind: "done", result: JSON.parse(message.data) as GenerateResult });
        } else if (message.event === "error") {
          finished = true;
          const failure = JSON.parse(message.data) as { code?: string; message?: string };
          dispatch({
            kind: "error",
            error: {
              status: null,
              code: failure.code ?? "stream_error",
              message: failure.message ?? "The stream ended with an error.",
            },
          });
        }
      }
    }
    if (!finished) {
      dispatch({
        kind: "error",
        error: {
          status: null,
          code: "stream_closed",
          message: "The stream ended before the request finished.",
        },
      });
    }
  } catch (caught) {
    if (signal.aborted) {
      dispatch({ kind: "cancelled" });
      return;
    }
    dispatch({
      kind: "error",
      error: {
        status: null,
        code: "network_error",
        message: caught instanceof Error ? caught.message : "The request could not be sent.",
      },
    });
  }
}
