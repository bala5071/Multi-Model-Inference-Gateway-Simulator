import { useState } from "react";
import { isOpen, ruleBody, ruleRows } from "./alerts";
import { ApiError, applicationPath, probeRefusedBody, type WriteProbe } from "./api";
import { Chooser } from "./Chooser";
import type { ConsoleContext } from "./context";
import { canWrite, refusalText } from "./derive";
import { EvaluatorBanner } from "./EvaluatorBanner";
import { RuleEditor, type Editing } from "./RuleEditor";
import { useLoad } from "./useLoad";
import type {
  AlertRule,
  ChannelName,
  Incident,
  IncidentPage,
  RulePage,
  RuleValidation,
} from "./types";
import { TableScroll } from "../components/TableScroll";
import { Toasted } from "../ui/Toasts";

interface Loaded {
  rules: AlertRule[];
  previews: Record<string, RuleValidation | null>;
  incidents: Incident[];
  channels: ChannelName[];
  probe: WriteProbe;
}

async function loadAll(context: ConsoleContext, app: string): Promise<Loaded> {
  const base = `${app}/alert-rules`;
  const page = await context.api.get<RulePage>(`${base}?limit=100`);
  const [previews, incidents, channels, probe] = await Promise.all([
    // Each rule's last hour, from the gateway's own dry run of its body (writes nothing).
    Promise.all(
      page.rules.map(async (rule) => {
        try {
          return [
            rule.id,
            await context.api.send<RuleValidation>("POST", `${base}/validate`, ruleBody(rule)),
          ] as const;
        } catch {
          return [rule.id, null] as const;
        }
      }),
    ).then((pairs) => Object.fromEntries(pairs)),
    context.api
      .get<IncidentPage>(`${app}/incidents?limit=100`)
      .then((p) => p.incidents.filter(isOpen))
      .catch(() => []),
    context.api
      .get<{ channels: ChannelName[] }>(`${app}/alert-channels`)
      .then((c) => c.channels)
      .catch(() => []),
    probeRefusedBody(context.api, base),
  ]);
  return { rules: page.rules, previews, incidents, channels, probe };
}

/**
 * One application's alert rules: each with its state and last value, defaults marked, and,
 * when the gateway says this account may change them, the editor with its last-hour preview,
 * enable and disable, delete, and restore-defaults.
 */
export function RulesScreen({
  context,
  applicationId,
  onChoose,
}: {
  context: ConsoleContext;
  applicationId: string | null;
  onChoose: (id: string) => void;
}) {
  const [reloads, setReloads] = useState(0);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<Editing | null>(null);
  const app =
    applicationId === null ? null : applicationPath(context.user.organization_id, applicationId);
  const loaded = useLoad(`rules:${app ?? ""}:${reloads}`, () =>
    app === null ? Promise.reject(new ApiError(0, "none", "")) : loadAll(context, app),
  );
  const base = app === null ? "" : `${app}/alert-rules`;

  function reload(note: string | null) {
    setMessage(note);
    setBusy(false);
    setEditing(null);
    setReloads((n) => n + 1);
  }

  async function act(what: string, method: "POST" | "DELETE", path: string, body?: unknown) {
    setBusy(true);
    setMessage(null);
    setError(null);
    try {
      await context.guarded(() => context.api.send(method, path, body));
      reload(what);
    } catch (failure) {
      if (failure instanceof ApiError) setError(refusalText(failure.code, failure.message));
      if (failure instanceof ApiError && failure.code === "rule_version_conflict") reload(null);
      setBusy(false);
    }
  }

  const writable = loaded.kind === "ready" && canWrite(loaded.value.probe);
  return (
    <div className="console-screen">
      <h2>Alert rules</h2>
      <EvaluatorBanner api={context.api} />
      <Chooser
        context={context}
        selected={applicationId}
        onChoose={(id) => {
          if (id !== null) onChoose(id);
        }}
      />
      <Toasted message={message} />
      {error !== null && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
      {app !== null && loaded.kind === "loading" && <p className="muted">Loading…</p>}
      {app !== null && loaded.kind === "failed" && (
        <p className="form-error" role="alert">
          {refusalText(loaded.error.code, loaded.error.message)}
        </p>
      )}
      {app !== null && loaded.kind === "ready" && (
        <>
          <dl className="facts">
            <div>
              <dt>Rules</dt>
              <dd>{loaded.value.rules.length}</dd>
            </div>
            <div>
              <dt>Access</dt>
              <dd>{writable ? "May change" : "Read only"}</dd>
            </div>
          </dl>
          <section className="panel" aria-label="Rules">
            <h3>Rules</h3>
            {loaded.value.rules.length === 0 ? (
              <p className="muted">This application has no alert rule.</p>
            ) : (
              <TableScroll table="Rules">
                <table className="data-table" aria-label="Rules">
                  <thead>
                    <tr>
                      <th scope="col">Rule</th>
                      <th scope="col">Default</th>
                      <th scope="col">Condition</th>
                      <th scope="col">Severity</th>
                      <th scope="col">Channels</th>
                      <th scope="col">State</th>
                      <th scope="col">Last value</th>
                      {writable && <th scope="col">Change</th>}
                    </tr>
                  </thead>
                  <tbody>
                    {ruleRows(
                      loaded.value.rules,
                      loaded.value.previews,
                      loaded.value.incidents,
                      loaded.value.channels,
                    ).map((row) => {
                      const rule = loaded.value.rules.find((r) => r.id === row.id);
                      return (
                        <tr key={row.id}>
                          <th scope="row">{row.name}</th>
                          <td>{row.mark === "" ? "-" : row.mark}</td>
                          <td>{row.condition}</td>
                          <td>{row.severity}</td>
                          <td>{row.channels}</td>
                          <td>{row.state}</td>
                          <td>{row.last}</td>
                          {writable && rule !== undefined && (
                            <td>
                              <div className="row-actions">
                                <button
                                  type="button"
                                  disabled={busy}
                                  onClick={() => {
                                    setEditing({ rule, body: ruleBody(rule) });
                                  }}
                                  aria-label={`Edit ${row.name}`}
                                >
                                  Edit
                                </button>
                                <button
                                  type="button"
                                  disabled={busy}
                                  onClick={() => {
                                    setEditing({
                                      rule: null,
                                      body: { ...ruleBody(rule), name: `Copy of ${rule.name}` },
                                    });
                                  }}
                                  aria-label={`Copy ${row.name}`}
                                >
                                  Copy
                                </button>
                                <button
                                  type="button"
                                  disabled={busy}
                                  onClick={() => {
                                    void act(
                                      `${rule.enabled ? "Disabled" : "Enabled"} ${rule.name}.`,
                                      "POST",
                                      `${base}/${encodeURIComponent(rule.id)}/${rule.enabled ? "disable" : "enable"}`,
                                      { expected_version: rule.version },
                                    );
                                  }}
                                  aria-label={`${rule.enabled ? "Disable" : "Enable"} ${row.name}`}
                                >
                                  {rule.enabled ? "Disable" : "Enable"}
                                </button>
                                <button
                                  type="button"
                                  disabled={busy}
                                  onClick={() => {
                                    void act(
                                      `Deleted ${rule.name}.`,
                                      "DELETE",
                                      `${base}/${encodeURIComponent(rule.id)}`,
                                      { expected_version: rule.version },
                                    );
                                  }}
                                  aria-label={`Delete ${row.name}`}
                                >
                                  Delete
                                </button>
                              </div>
                            </td>
                          )}
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </TableScroll>
            )}
            {writable && (
              <div className="form-actions">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => {
                    void act("Restored the default rules.", "POST", `${base}/restore-defaults`);
                  }}
                >
                  Restore default rules
                </button>
              </div>
            )}
          </section>
          {writable && editing !== null && (
            <RuleEditor
              key={`${editing.rule?.id ?? "new"}:${String(editing.rule?.version ?? 0)}`}
              context={context}
              base={base}
              editing={editing}
              onSaved={(what) => {
                reload(what);
              }}
              onConflict={() => {
                reload(null);
              }}
              onCancel={() => {
                setEditing(null);
              }}
            />
          )}
        </>
      )}
    </div>
  );
}
