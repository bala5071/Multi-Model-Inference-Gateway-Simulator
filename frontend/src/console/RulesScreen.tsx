import { useState } from "react";
import { isOpen, ruleBody, ruleRows, type RuleRow } from "./alerts";
import { ApiError, applicationPath, probeRefusedBody, type WriteProbe } from "./api";
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
import { Button } from "../ui/Button";
import { Card, StatTile, StatTiles } from "../ui/Card";
import { DataTable } from "../ui/DataTable";
import { Skeleton } from "../ui/Skeleton";
import { StatusPill, toneOf } from "../ui/StatusPill";
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
}: {
  context: ConsoleContext;
  applicationId: string;
}) {
  const [reloads, setReloads] = useState(0);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<Editing | null>(null);
  const app = applicationPath(context.user.organization_id, applicationId);
  const loaded = useLoad(`rules:${app}:${reloads}`, () => loadAll(context, app));
  const base = `${app}/alert-rules`;

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
      <EvaluatorBanner api={context.api} />
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
          <StatTiles>
            <StatTile label="Rules" value={loaded.value.rules.length} />
            <StatTile label="Access" value={writable ? "May change" : "Read only"} />
          </StatTiles>
          <Card
            title="Rules"
            label="Rules"
            actions={
              writable && (
                <Button
                  disabled={busy}
                  onClick={() => {
                    void act("Restored the default rules.", "POST", `${base}/restore-defaults`);
                  }}
                >
                  Restore default rules
                </Button>
              )
            }
          >
            <DataTable
              label="Rules"
              rows={ruleRows(
                loaded.value.rules,
                loaded.value.previews,
                loaded.value.incidents,
                loaded.value.channels,
              )}
              rowKey={(row) => row.id}
              empty="This application has no alert rule."
              columns={[
                {
                  key: "name",
                  header: "Rule",
                  cell: (r) => r.name,
                  rowHeader: true,
                  sortValue: (r) => r.name,
                },
                { key: "mark", header: "Default", cell: (r) => (r.mark === "" ? "-" : r.mark) },
                { key: "condition", header: "Condition", cell: (r) => r.condition },
                {
                  key: "severity",
                  header: "Severity",
                  cell: (r) => r.severity,
                  sortValue: (r) => r.severity,
                },
                { key: "channels", header: "Channels", cell: (r) => r.channels },
                {
                  key: "state",
                  header: "State",
                  cell: (r) => <StatusPill tone={toneOf(r.state)}>{r.state}</StatusPill>,
                  sortValue: (r) => r.state,
                },
                { key: "last", header: "Last value", cell: (r) => r.last },
                ...(writable
                  ? [
                      {
                        key: "change",
                        header: "Change",
                        cell: (row: RuleRow) => {
                          const rule = loaded.value.rules.find((r) => r.id === row.id);
                          if (rule === undefined) return null;
                          return (
                            <div className="row-actions">
                              <Button
                                size="sm"
                                disabled={busy}
                                aria-label={`Edit ${row.name}`}
                                onClick={() => {
                                  setEditing({ rule, body: ruleBody(rule) });
                                }}
                              >
                                Edit
                              </Button>
                              <Button
                                size="sm"
                                disabled={busy}
                                aria-label={`Copy ${row.name}`}
                                onClick={() => {
                                  setEditing({
                                    rule: null,
                                    body: { ...ruleBody(rule), name: `Copy of ${rule.name}` },
                                  });
                                }}
                              >
                                Copy
                              </Button>
                              <Button
                                size="sm"
                                disabled={busy}
                                aria-label={`${rule.enabled ? "Disable" : "Enable"} ${row.name}`}
                                onClick={() => {
                                  void act(
                                    `${rule.enabled ? "Disabled" : "Enabled"} ${rule.name}.`,
                                    "POST",
                                    `${base}/${encodeURIComponent(rule.id)}/${rule.enabled ? "disable" : "enable"}`,
                                    { expected_version: rule.version },
                                  );
                                }}
                              >
                                {rule.enabled ? "Disable" : "Enable"}
                              </Button>
                              <Button
                                size="sm"
                                variant="danger"
                                disabled={busy}
                                aria-label={`Delete ${row.name}`}
                                onClick={() => {
                                  void act(
                                    `Deleted ${rule.name}.`,
                                    "DELETE",
                                    `${base}/${encodeURIComponent(rule.id)}`,
                                    { expected_version: rule.version },
                                  );
                                }}
                              >
                                Delete
                              </Button>
                            </div>
                          );
                        },
                      },
                    ]
                  : []),
              ]}
            />
          </Card>
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
