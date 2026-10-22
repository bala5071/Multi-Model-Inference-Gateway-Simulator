import { useEffect, useReducer, useRef, useState } from "react";
import {
  IDLE_RUN,
  QUALITIES,
  reduceRun,
  requestBody,
  streamGenerate,
  tokenGaps,
  ttft,
  type PlaygroundForm,
  type Quality,
  type Run,
} from "../api/playground";
import type { MonitorState } from "../api/store";
import { formatValue } from "../charts/transforms";
import { ItlBars } from "../components/ItlBars";
import { LoadControl } from "../components/LoadControl";
import type { Control } from "../hooks/useControl";

const PHASE_TEXT: Record<Run["phase"], string> = {
  idle: "Nothing sent yet.",
  sending: "Sent. Waiting for the gateway…",
  streaming: "Streaming…",
  done: "Finished.",
  failed: "Failed.",
  cancelled: "Cancelled.",
};

function Header({ name, value, absent }: { name: string; value: string | null; absent: string }) {
  return (
    <tr>
      <th scope="row">
        <code>{name}</code>
      </th>
      <td>{value ?? <span className="muted">{absent}</span>}</td>
    </tr>
  );
}

function Output({ run }: { run: Run }) {
  const first = ttft(run.tokens);
  const result = run.result;
  return (
    <section className="panel" aria-label="Response">
      <p role="status" aria-label="Request status">
        {PHASE_TEXT[run.phase]}
      </p>
      {run.error !== null && (
        <p className="form-error" role="alert">
          <strong>
            {run.error.status !== null && `HTTP ${run.error.status} `}
            {run.error.code}
          </strong>
          : {run.error.message}
        </p>
      )}
      {run.phase !== "idle" &&
        run.phase !== "sending" &&
        (run.error === null || run.error.status === null) && (
          <>
            <table className="data-table headers">
              <caption>Response headers</caption>
              <tbody>
                <Header name="X-Model-Used" value={run.headers.modelUsed} absent="not sent" />
                <Header
                  name="X-Downgraded-From"
                  value={run.headers.downgradedFrom}
                  absent="not sent: the request was not downgraded"
                />
                <Header
                  name="X-Cost-USD"
                  value={run.headers.costUsd}
                  absent={
                    result === null
                      ? "not sent on a streamed response: the cost is not known until it ends"
                      : `not sent on a streamed response; the final event gives $${result.cost.usd.toFixed(10)}`
                  }
                />
              </tbody>
            </table>
            <dl className="facts">
              <div>
                <dt>Time to first token</dt>
                <dd>
                  {formatValue(first)} <span className="unit">ms</span>
                </dd>
              </div>
              <div>
                <dt>Tokens</dt>
                <dd>{run.tokens.length}</dd>
              </div>
              {result !== null && (
                <>
                  <div>
                    <dt>Total</dt>
                    <dd>
                      {formatValue(result.timing.total_ms)} <span className="unit">ms</span>
                    </dd>
                  </div>
                  <div>
                    <dt>Queue wait</dt>
                    <dd>
                      {formatValue(result.timing.queue_ms)} <span className="unit">ms</span>
                    </dd>
                  </div>
                  <div>
                    <dt>Prompt tokens</dt>
                    <dd>
                      {result.usage.prompt_tokens}{" "}
                      <span className="unit">({result.usage.cached_tokens} cached)</span>
                    </dd>
                  </div>
                </>
              )}
            </dl>
            <p className="muted">
              Times are in ms after the request arrived, on the gateway clock.
            </p>
            <ItlBars gaps={tokenGaps(run.tokens)} />
            <h4>Tokens as they arrived</h4>
            <p className="tokens" aria-label="Tokens" aria-live="off">
              {run.tokens.map((token) => (
                <span
                  key={token.index}
                  title={`token ${token.index} at ${formatValue(token.t_ms)} ms`}
                >
                  {token.token}{" "}
                </span>
              ))}
            </p>
          </>
        )}
    </section>
  );
}

export function Playground({ state, control }: { state: MonitorState; control: Control }) {
  const snapshot = state.snapshot;
  const models = Object.keys(snapshot?.models ?? {});
  const tenants = Object.keys(snapshot?.tenants.tenants ?? {});
  const [edited, setEdited] = useState<Partial<PlaygroundForm>>({});
  const [run, dispatch] = useReducer(reduceRun, IDLE_RUN);
  const abort = useRef<AbortController | null>(null);

  useEffect(
    () => () => {
      abort.current?.abort();
    },
    [],
  );

  // Until the viewer chooses, the first tenant and model the gateway lists.
  const form: PlaygroundForm = {
    tenant: tenants[0] ?? "",
    choose: "model",
    model: models[0] ?? "",
    quality: "balanced",
    allowDowngrade: false,
    maxTokens: "32",
    prompt: "Explain what a prefix cache saves, in one paragraph.",
    ...edited,
  };
  const set = (change: Partial<PlaygroundForm>) => {
    setEdited({ ...edited, ...change });
  };
  const busy = run.phase === "sending" || run.phase === "streaming";

  return (
    <div className="playground">
      <section aria-labelledby="playground-request">
        <h3 id="playground-request">Send a request</h3>
        <form
          className="request-form"
          onSubmit={(event) => {
            event.preventDefault();
            const controller = new AbortController();
            abort.current = controller;
            void streamGenerate(requestBody(form), dispatch, controller.signal);
          }}
        >
          <div className="filters">
            {tenants.length > 0 && (
              <label className="choice">
                Tenant
                <select
                  value={form.tenant}
                  onChange={(event) => {
                    set({ tenant: event.target.value });
                  }}
                >
                  {tenants.map((tenant) => (
                    <option key={tenant} value={tenant}>
                      {tenant} ({snapshot?.tenants.tenants?.[tenant]?.plan})
                    </option>
                  ))}
                </select>
              </label>
            )}
            <label className="choice">
              Choose by
              <select
                value={form.choose}
                onChange={(event) => {
                  set({ choose: event.target.value === "quality" ? "quality" : "model" });
                }}
              >
                <option value="model">Model</option>
                <option value="quality">Quality</option>
              </select>
            </label>
            {form.choose === "model" ? (
              <label className="choice">
                Model
                <select
                  value={form.model}
                  onChange={(event) => {
                    set({ model: event.target.value });
                  }}
                >
                  {models.map((model) => (
                    <option key={model} value={model}>
                      {model}
                    </option>
                  ))}
                </select>
              </label>
            ) : (
              <label className="choice">
                Quality
                <select
                  value={form.quality}
                  onChange={(event) => {
                    set({ quality: event.target.value as Quality });
                  }}
                >
                  {QUALITIES.map((quality) => (
                    <option key={quality} value={quality}>
                      {quality}
                    </option>
                  ))}
                </select>
              </label>
            )}
            <label className="choice">
              Max tokens
              <input
                type="number"
                min={1}
                value={form.maxTokens}
                onChange={(event) => {
                  set({ maxTokens: event.target.value });
                }}
              />
            </label>
            <label className="check">
              <input
                type="checkbox"
                checked={form.allowDowngrade}
                onChange={(event) => {
                  set({ allowDowngrade: event.target.checked });
                }}
              />
              Allow downgrade
            </label>
          </div>
          <label className="choice">
            Prompt
            <textarea
              rows={3}
              value={form.prompt}
              onChange={(event) => {
                set({ prompt: event.target.value });
              }}
            />
          </label>
          <div className="filters">
            <button type="submit" disabled={busy || form.prompt.trim() === "" || snapshot === null}>
              Send
            </button>
            <button
              type="button"
              disabled={!busy}
              onClick={() => {
                abort.current?.abort();
              }}
            >
              Cancel
            </button>
          </div>
        </form>
        <Output run={run} />
      </section>

      <section aria-labelledby="playground-load">
        <h3 id="playground-load">Load</h3>
        <LoadControl control={control} />
      </section>
    </div>
  );
}
