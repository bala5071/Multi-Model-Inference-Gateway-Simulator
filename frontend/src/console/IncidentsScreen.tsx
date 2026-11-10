import { useState } from "react";
import { followsProbe, incidentRows, incidentTimeline, notificationRows } from "./alerts";
import { ApiError, applicationPath, orgPath, probeRefusedBody, type WriteProbe } from "./api";
import { Chooser } from "./Chooser";
import type { ConsoleContext } from "./context";
import { refusalText } from "./derive";
import { EvaluatorBanner } from "./EvaluatorBanner";
import { useLoad } from "./useLoad";
import type { ChannelName, IncidentDetail, IncidentPage, RulePage } from "./types";

/** The states the gateway filters by (docs/45); empty is every state. */
const STATES = ["", "pending", "firing", "acknowledged", "resolved"];

async function ruleNamesOf(
  context: ConsoleContext,
  applications: readonly string[],
): Promise<Record<string, Record<string, string>>> {
  const org = context.user.organization_id;
  const pairs = await Promise.all(
    applications.map(async (app) => {
      try {
        const page = await context.api.get<RulePage>(
          `${applicationPath(org, app)}/alert-rules?limit=100`,
        );
        return [app, Object.fromEntries(page.rules.map((r) => [r.id, r.name]))] as const;
      } catch {
        return [app, {}] as const;
      }
    }),
  );
  return Object.fromEntries(pairs);
}

function IncidentList({
  context,
  applicationId,
  onOpen,
}: {
  context: ConsoleContext;
  applicationId: string | null;
  onOpen: (applicationId: string, incidentId: string) => void;
}) {
  const [state, setState] = useState("");
  const org = context.user.organization_id;
  const query = `?limit=100${state === "" ? "" : `&state=${encodeURIComponent(state)}`}`;
  const path =
    applicationId === null
      ? `${orgPath(org)}/incidents${query}`
      : `${applicationPath(org, applicationId)}/incidents${query}`;
  const loaded = useLoad(`incidents:${path}`, async () => {
    const page = await context.api.get<IncidentPage>(path);
    const apps = [
      ...new Set(
        page.incidents.flatMap((i) => (i.application_id === null ? [] : [i.application_id])),
      ),
    ];
    return { page, names: await ruleNamesOf(context, apps) };
  });

  return (
    <section className="panel" aria-label="Incidents">
      <h3>{applicationId === null ? "Incidents in the organization" : "Incidents"}</h3>
      <label className="chooser">
        State
        <select
          value={state}
          onChange={(event) => {
            setState(event.target.value);
          }}
        >
          {STATES.map((s) => (
            <option key={s} value={s}>
              {s === "" ? "Every state" : s}
            </option>
          ))}
        </select>
      </label>
      {loaded.kind === "loading" && <p className="muted">Loading…</p>}
      {loaded.kind === "failed" && (
        <p className="form-error" role="alert">
          {refusalText(loaded.error.code, loaded.error.message)}
        </p>
      )}
      {loaded.kind === "ready" &&
        (loaded.value.page.incidents.length === 0 ? (
          <p className="muted">No incident.</p>
        ) : (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">Rule</th>
                  <th scope="col">Application</th>
                  <th scope="col">State</th>
                  <th scope="col">Severity</th>
                  <th scope="col">Value</th>
                  <th scope="col">Opened</th>
                  <th scope="col">Resolved</th>
                  <th scope="col">Details</th>
                </tr>
              </thead>
              <tbody>
                {incidentRows(loaded.value.page.incidents, loaded.value.names).map((row) => (
                  <tr key={row.id}>
                    <th scope="row">{row.rule}</th>
                    <td>{row.application}</td>
                    <td>{row.state}</td>
                    <td>{row.severity}</td>
                    <td>{row.value}</td>
                    <td>{row.opened}</td>
                    <td>{row.resolved}</td>
                    <td>
                      {row.applicationId !== null && (
                        <button
                          type="button"
                          onClick={() => {
                            if (row.applicationId !== null) onOpen(row.applicationId, row.id);
                          }}
                        >
                          Open {row.rule}
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))}
    </section>
  );
}

interface Detail {
  incident: IncidentDetail;
  rule: string;
  channels: ChannelName[];
  probe: WriteProbe;
}

function IncidentView({
  context,
  applicationId,
  incidentId,
  onBack,
}: {
  context: ConsoleContext;
  applicationId: string;
  incidentId: string;
  onBack: () => void;
}) {
  const [reloads, setReloads] = useState(0);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const app = applicationPath(context.user.organization_id, applicationId);
  const path = `${app}/incidents/${encodeURIComponent(incidentId)}`;
  const loaded = useLoad(`incident:${path}:${reloads}`, async (): Promise<Detail> => {
    const incident = await context.api.get<IncidentDetail>(path);
    const [names, channels, probe] = await Promise.all([
      ruleNamesOf(context, [applicationId]),
      context.api
        .get<{ channels: ChannelName[] }>(`${app}/alert-channels`)
        .then((c) => c.channels)
        .catch(() => []),
      // Acknowledge has no dry run: it follows the rule probe of this application (note 49).
      probeRefusedBody(context.api, `${app}/alert-rules`),
    ]);
    return {
      incident,
      rule: names[applicationId]?.[incident.rule_id] ?? incident.rule_id,
      channels,
      probe,
    };
  });

  async function acknowledge() {
    setBusy(true);
    setMessage(null);
    setError(null);
    try {
      await context.guarded(() => context.api.send("POST", `${path}/acknowledge`));
      setMessage("Acknowledged.");
    } catch (failure) {
      if (failure instanceof ApiError) setError(refusalText(failure.code, failure.message));
    }
    setBusy(false);
    setReloads((n) => n + 1);
  }

  return (
    <section className="panel" aria-label="Incident">
      <div className="form-actions">
        <button type="button" onClick={onBack}>
          Back to the list
        </button>
      </div>
      {message !== null && (
        <p className="notice" role="status">
          {message}
        </p>
      )}
      {error !== null && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
      {loaded.kind === "loading" && <p className="muted">Loading…</p>}
      {loaded.kind === "failed" && (
        <p className="form-error" role="alert">
          {refusalText(loaded.error.code, loaded.error.message)}
        </p>
      )}
      {loaded.kind === "ready" && (
        <>
          <h3>{loaded.value.rule}</h3>
          <dl className="facts">
            <div>
              <dt>State</dt>
              <dd>{loaded.value.incident.state}</dd>
            </div>
            <div>
              <dt>Severity</dt>
              <dd>{loaded.value.incident.severity}</dd>
            </div>
            <div>
              <dt>Application</dt>
              <dd>{applicationId}</dd>
            </div>
            <div>
              <dt>Series</dt>
              <dd>{loaded.value.incident.series ?? "-"}</dd>
            </div>
          </dl>
          {loaded.value.incident.state === "firing" && followsProbe(loaded.value.probe) && (
            <div className="form-actions">
              <button
                type="button"
                disabled={busy}
                onClick={() => {
                  void acknowledge();
                }}
              >
                Acknowledge
              </button>
            </div>
          )}
          <h4>Timeline</h4>
          <ol className="timeline" aria-label="Timeline">
            {incidentTimeline(loaded.value.incident).map(([at, what]) => (
              <li key={`${at}:${what}`}>
                <span className="muted">{at}</span> {what}
              </li>
            ))}
          </ol>
          <h4>Notifications</h4>
          {loaded.value.incident.notifications.length === 0 ? (
            <p className="muted">No notification.</p>
          ) : (
            <div className="table-scroll">
              <table className="data-table" aria-label="Notifications">
                <thead>
                  <tr>
                    <th scope="col">Kind</th>
                    <th scope="col">State</th>
                    <th scope="col">At</th>
                    <th scope="col">Reason</th>
                    <th scope="col">Deliveries</th>
                  </tr>
                </thead>
                <tbody>
                  {notificationRows(loaded.value.incident, loaded.value.channels).map((n) => (
                    <tr key={`${n.at}:${n.kind}`}>
                      <td>{n.kind}</td>
                      <td>{n.state}</td>
                      <td>{n.at}</td>
                      <td>{n.reason}</td>
                      <td>{n.deliveries}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </section>
  );
}

/**
 * Incidents, open and recent: the organization's, or one application's (for an account that
 * may not read the organization's, the application it names), each with its timeline, its
 * notifications and their deliveries, and Acknowledge where the gateway says this account may.
 */
export function IncidentsScreen({
  context,
  applicationId,
  incidentId,
  onRoute,
}: {
  context: ConsoleContext;
  applicationId: string | null;
  incidentId: string | null;
  onRoute: (applicationId: string | null, incidentId: string | null) => void;
}) {
  return (
    <div className="console-screen">
      <h2>Incidents</h2>
      <EvaluatorBanner api={context.api} />
      {applicationId !== null && incidentId !== null ? (
        <IncidentView
          context={context}
          applicationId={applicationId}
          incidentId={incidentId}
          onBack={() => {
            onRoute(applicationId, null);
          }}
        />
      ) : (
        <>
          <Chooser
            all
            context={context}
            selected={applicationId}
            onChoose={(id) => {
              onRoute(id, null);
            }}
          />
          <IncidentList
            key={applicationId ?? "*"}
            context={context}
            applicationId={applicationId}
            onOpen={(app, id) => {
              onRoute(app, id);
            }}
          />
        </>
      )}
    </div>
  );
}
