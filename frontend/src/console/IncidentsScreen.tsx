import { useId, useState } from "react";
import { followsProbe, incidentRows, incidentTimeline, notificationRows } from "./alerts";
import { ApiError, applicationPath, orgPath, probeRefusedBody, type WriteProbe } from "./api";
import { Chooser } from "./Chooser";
import type { ConsoleContext } from "./context";
import { refusalText } from "./derive";
import { EvaluatorBanner } from "./EvaluatorBanner";
import { useLoad } from "./useLoad";
import type { ChannelName, IncidentDetail, IncidentPage, RulePage } from "./types";
import { Button } from "../ui/Button";
import { Card, StatTile, StatTiles } from "../ui/Card";
import { DataTable } from "../ui/DataTable";
import { Skeleton } from "../ui/Skeleton";
import { StatusPill, toneOf } from "../ui/StatusPill";
import { TabPanel, Tabs } from "../ui/Tabs";
import { Toasted } from "../ui/Toasts";

/** The states the gateway filters by (docs/45), as tabs; "all" asks for every state. */
const STATES = [
  { id: "all", label: "Every state" },
  { id: "pending", label: "pending" },
  { id: "firing", label: "firing" },
  { id: "acknowledged", label: "acknowledged" },
  { id: "resolved", label: "resolved" },
] as const;
type StateTab = (typeof STATES)[number]["id"];

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
  const [state, setState] = useState<StateTab>("all");
  const tabs = useId();
  const org = context.user.organization_id;
  const query = `?limit=100${state === "all" ? "" : `&state=${encodeURIComponent(state)}`}`;
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
    <Card
      label="Incidents"
      title={applicationId === null ? "Incidents in the organization" : "Incidents"}
    >
      <Tabs label="State" prefix={tabs} tabs={STATES} selected={state} onSelect={setState} />
      <TabPanel prefix={tabs} selected={state}>
        {loaded.kind === "failed" ? (
          <p className="form-error" role="alert">
            {refusalText(loaded.error.code, loaded.error.message)}
          </p>
        ) : (
          <DataTable
            label="Incidents"
            loading={loaded.kind === "loading"}
            rows={
              loaded.kind === "ready"
                ? incidentRows(loaded.value.page.incidents, loaded.value.names)
                : []
            }
            rowKey={(row) => row.id}
            empty="No incident."
            columns={[
              {
                key: "rule",
                header: "Rule",
                cell: (r) => r.rule,
                rowHeader: true,
                sortValue: (r) => r.rule,
              },
              {
                key: "app",
                header: "Application",
                cell: (r) => r.application,
                sortValue: (r) => r.application,
              },
              {
                key: "state",
                header: "State",
                cell: (r) => <StatusPill tone={toneOf(r.state)}>{r.state}</StatusPill>,
                sortValue: (r) => r.state,
              },
              { key: "severity", header: "Severity", cell: (r) => r.severity },
              { key: "value", header: "Value", cell: (r) => r.value },
              {
                key: "opened",
                header: "Opened",
                cell: (r) => r.opened,
                sortValue: (r) => r.opened,
              },
              { key: "resolved", header: "Resolved", cell: (r) => r.resolved },
              {
                key: "details",
                header: "Details",
                cell: (r) =>
                  r.applicationId !== null && (
                    <Button
                      size="sm"
                      onClick={() => {
                        if (r.applicationId !== null) onOpen(r.applicationId, r.id);
                      }}
                    >
                      Open {r.rule}
                    </Button>
                  ),
              },
            ]}
          />
        )}
      </TabPanel>
    </Card>
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
        <Button variant="quiet" onClick={onBack}>
          Back to the list
        </Button>
      </div>
      <Toasted message={message} />
      {error !== null && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
      {loaded.kind === "loading" && <Skeleton />}
      {loaded.kind === "failed" && (
        <p className="form-error" role="alert">
          {refusalText(loaded.error.code, loaded.error.message)}
        </p>
      )}
      {loaded.kind === "ready" && (
        <>
          <h3>{loaded.value.rule}</h3>
          <StatTiles>
            <StatTile
              label="State"
              value={
                <StatusPill tone={toneOf(loaded.value.incident.state)}>
                  {loaded.value.incident.state}
                </StatusPill>
              }
            />
            <StatTile label="Severity" value={loaded.value.incident.severity} />
            <StatTile label="Application" value={applicationId} />
            <StatTile label="Series" value={loaded.value.incident.series ?? "-"} />
          </StatTiles>
          {loaded.value.incident.state === "firing" && followsProbe(loaded.value.probe) && (
            <div className="form-actions">
              <Button
                variant="primary"
                loading={busy}
                loadingText="Acknowledging…"
                onClick={() => {
                  void acknowledge();
                }}
              >
                Acknowledge
              </Button>
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
          <DataTable
            label="Notifications"
            rows={notificationRows(loaded.value.incident, loaded.value.channels)}
            rowKey={(n) => `${n.at}:${n.kind}`}
            empty="No notification."
            columns={[
              { key: "kind", header: "Kind", cell: (n) => n.kind },
              {
                key: "state",
                header: "State",
                cell: (n) => <StatusPill tone={toneOf(n.state)}>{n.state}</StatusPill>,
              },
              { key: "at", header: "At", cell: (n) => n.at },
              { key: "reason", header: "Reason", cell: (n) => n.reason },
              { key: "deliveries", header: "Deliveries", cell: (n) => n.deliveries },
            ]}
          />
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
