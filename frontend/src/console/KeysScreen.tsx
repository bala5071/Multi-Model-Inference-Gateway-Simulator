import { useState } from "react";
import { ApiError, applicationPath } from "./api";
import { Chooser } from "./Chooser";
import type { ConsoleContext } from "./context";
import { formatTime, refusalText } from "./derive";
import { KeyShownOnce } from "./KeyShownOnce";
import { useLoad } from "./useLoad";
import type { ApiKeySummary, IssuedKey, KeyPage } from "./types";

function failure(error: unknown): string {
  if (error instanceof ApiError) return refusalText(error.code, error.message);
  return "Something went wrong reading the gateway's answer.";
}

function when(ms: number | null, none: string): string {
  return ms === null ? none : formatTime(ms);
}

/**
 * An application's API keys (docs/51): the list, and Issue, Rotate and Revoke. Those three are
 * shown once the gateway has answered the list: their rows equal the list's
 * (`application_keys.list`), pinned by tests/test_console_probes.py, since a probe of a step-up
 * write would be an audited refusal. Each still asks for the password (step-up), and the gateway
 * still checks it. A new key is shown once and forgotten.
 */
export function KeysScreen({
  context,
  applicationId,
  onChoose,
}: {
  context: ConsoleContext;
  applicationId: string | null;
  onChoose: (id: string) => void;
}) {
  return (
    <section className="console-screen panel" aria-labelledby="keys-title">
      <h2 id="keys-title">API keys</h2>
      <Chooser
        context={context}
        selected={applicationId}
        onChoose={(id) => {
          if (id !== null) onChoose(id);
        }}
      />
      {applicationId !== null && (
        <ApplicationKeys key={applicationId} context={context} applicationId={applicationId} />
      )}
    </section>
  );
}

function ApplicationKeys({
  context,
  applicationId,
}: {
  context: ConsoleContext;
  applicationId: string;
}) {
  const base = `${applicationPath(context.user.organization_id, applicationId)}/keys`;
  const [version, setVersion] = useState(0);
  const listed = useLoad(`keys:${base}:${version.toString()}`, () =>
    context.api.get<KeyPage>(`${base}?limit=100`),
  );
  const [shown, setShown] = useState<IssuedKey | null>(null);
  const [label, setLabel] = useState("");
  const [graceHours, setGraceHours] = useState("24");
  const [revoking, setRevoking] = useState<ApiKeySummary | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function act(run: () => Promise<IssuedKey | null>) {
    setBusy(true);
    setError(null);
    try {
      const issued = await context.guarded(run);
      if (issued !== null) setShown(issued);
      setVersion((v) => v + 1);
    } catch (caught) {
      setError(failure(caught));
    } finally {
      setBusy(false);
      setRevoking(null);
    }
  }

  if (listed.kind === "loading") return <p className="muted">Loading keys…</p>;
  if (listed.kind === "failed") {
    return (
      <p className="form-error" role="alert">
        {refusalText(listed.error.code, listed.error.message)}
      </p>
    );
  }
  const keys = listed.value.keys;
  return (
    <>
      {shown !== null && (
        <KeyShownOnce
          value={shown.key}
          label={shown.label}
          onDone={() => {
            setShown(null);
          }}
        />
      )}
      {keys.length === 0 ? (
        <p className="muted">This application has no keys yet.</p>
      ) : (
        <div className="table-scroll">
          <table className="data-table" aria-label="Keys">
            <thead>
              <tr>
                <th scope="col">Label</th>
                <th scope="col">Key id</th>
                <th scope="col">State</th>
                <th scope="col">Created</th>
                <th scope="col">Expires</th>
                <th scope="col">Last used</th>
                <th scope="col">Change</th>
              </tr>
            </thead>
            <tbody>
              {keys.map((k) => (
                <tr key={k.key_id}>
                  <th scope="row">{k.label}</th>
                  <td>
                    <code className="figure">{k.key_id}</code>
                  </td>
                  <td>{k.state}</td>
                  <td>{formatTime(k.created_at_ms)}</td>
                  <td>{when(k.expires_at_ms, "never")}</td>
                  <td>{when(k.last_used_at_ms, "not yet")}</td>
                  <td>
                    {k.state !== "revoked" && (
                      <div className="form-actions">
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => {
                            const hours = Number(graceHours);
                            void act(() =>
                              context.api.send<IssuedKey>("POST", `${base}/${k.key_id}/rotate`, {
                                grace_hours: hours,
                              }),
                            );
                          }}
                        >
                          Rotate {k.label}
                        </button>
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => {
                            setRevoking(k);
                          }}
                        >
                          Revoke {k.label}
                        </button>
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {revoking !== null && (
        <div className="panel" role="alertdialog" aria-label="Revoke a key">
          <p>
            Revoke <strong>{revoking.label}</strong> ({revoking.key_id})? Requests with it are
            refused at once. This cannot be undone.
          </p>
          <div className="form-actions">
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                const id = revoking.key_id;
                void act(async () => {
                  await context.api.send("POST", `${base}/${id}/revoke`, {});
                  return null;
                });
              }}
            >
              Revoke
            </button>
            <button
              type="button"
              onClick={() => {
                setRevoking(null);
              }}
            >
              Cancel
            </button>
          </div>
        </div>
      )}
      <form
        className="panel"
        aria-label="Issue a key"
        onSubmit={(event) => {
          event.preventDefault();
          void act(() => context.api.send<IssuedKey>("POST", base, { label }));
        }}
      >
        <h3>Issue a key</h3>
        <label>
          Label
          <input
            value={label}
            maxLength={64}
            required
            onChange={(event) => {
              setLabel(event.target.value);
            }}
          />
        </label>
        <label>
          When rotating, keep the old key for (hours)
          <input
            type="number"
            min={0}
            max={168}
            value={graceHours}
            onChange={(event) => {
              setGraceHours(event.target.value);
            }}
          />
        </label>
        <div className="form-actions">
          <button type="submit" disabled={busy || label === ""}>
            Issue a key
          </button>
        </div>
        <p className="muted">Each change asks for your password again. A new key is shown once.</p>
      </form>
      {error !== null && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
    </>
  );
}
