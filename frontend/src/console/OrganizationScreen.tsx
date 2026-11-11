import { useState } from "react";
import { ApiError, applicationSettingsPath, orgPath, probeWrite, type WriteProbe } from "./api";
import type { ConsoleContext } from "./context";
import { canWrite, capRows, formatCaps, limitRows, refusalText } from "./derive";
import { EditForm } from "./EditForm";
import { History } from "./History";
import { displayValue, organizationDraft } from "./settingsForm";
import { useLoad } from "./useLoad";
import type {
  ApplicationPage,
  ApplicationSettings,
  ApplicationSummary,
  Change,
  OrganizationSettings,
  VersionPage,
} from "./types";
import { Card, StatTile, StatTiles } from "../ui/Card";
import { DataTable } from "../ui/DataTable";
import { Skeleton } from "../ui/Skeleton";
import { Toasted } from "../ui/Toasts";

interface Loaded {
  settings: OrganizationSettings;
  versions: VersionPage;
  applications: { summary: ApplicationSummary; settings: ApplicationSettings }[] | ApiError;
  probe: WriteProbe;
}

async function applicationsOf(
  context: ConsoleContext,
): Promise<{ summary: ApplicationSummary; settings: ApplicationSettings }[]> {
  const org = context.user.organization_id;
  const page = await context.api.get<ApplicationPage>(`${orgPath(org)}/applications?limit=100`);
  return Promise.all(
    page.applications.map(async (summary) => ({
      summary,
      settings: await context.api.get<ApplicationSettings>(
        applicationSettingsPath(org, summary.application_id),
      ),
    })),
  );
}

async function loadAll(context: ConsoleContext): Promise<Loaded> {
  const base = `${orgPath(context.user.organization_id)}/settings`;
  const settings = await context.api.get<OrganizationSettings>(base);
  const [versions, applications, probe] = await Promise.all([
    context.api.get<VersionPage>(`${base}/versions?limit=20`),
    applicationsOf(context).catch((error: unknown) =>
      error instanceof ApiError ? error : new ApiError(0, "unexpected", "Not read."),
    ),
    // Who may change the caps is asked of the gateway: a dry run of no change (docs/55).
    probeWrite(context.api, base, settings.version),
  ]);
  return { settings, versions, applications, probe };
}

/** Beside each cap in the form: what the organization set, and the platform's cap over it. */
function facts(settings: OrganizationSettings, name: string) {
  return (
    <>
      <span>Set: {displayValue(name, settings.settings[name])}</span>
      <span>Platform&apos;s cap: {formatCaps(settings.platform_caps[name] ?? {})}</span>
    </>
  );
}

/**
 * The organization's caps, the applications each one holds down now, and their history; and,
 * where the gateway's dry run says this account may write them, the typed form (docs/52 section
 * 16.7: the answer that superseded "it shows; it changes nothing") and Revert.
 */
export function OrganizationScreen({ context }: { context: ConsoleContext }) {
  const [reloads, setReloads] = useState(0);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const base = `${orgPath(context.user.organization_id)}/settings`;
  const loaded = useLoad(`organization:${context.user.organization_id}:${reloads}`, () =>
    loadAll(context),
  );

  function done(change: Change, what: string) {
    setMessage(`${what} as version ${change.version}.`);
    setError(null);
    setBusy(false);
    setReloads((n) => n + 1);
  }

  async function revert(version: number) {
    setBusy(true);
    setMessage(null);
    setError(null);
    try {
      const change = await context.guarded(() =>
        context.api.send<Change>("POST", `${base}/revert`, { expected_version: version }),
      );
      done(change, "Reverted");
    } catch (failure) {
      if (failure instanceof ApiError) setError(refusalText(failure.code, failure.message));
      if (failure instanceof ApiError && failure.code === "settings_version_conflict") {
        setReloads((n) => n + 1);
      }
      setBusy(false);
    }
  }

  return (
    <div className="console-screen">
      <h2>Organization settings</h2>
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
            <StatTile label="Organization" value={loaded.value.settings.organization_id} />
            <StatTile label="Version" value={loaded.value.settings.version} />
            <StatTile label="Mode" value={loaded.value.settings.mode} />
            <StatTile
              label="Access"
              value={canWrite(loaded.value.probe) ? "May change" : "Read only"}
            />
          </StatTiles>
          <Card title="Caps" label="Caps">
            <DataTable
              label="Caps"
              rows={capRows(loaded.value.settings)}
              rowKey={(row) => row.name}
              empty="No cap."
              columns={[
                { key: "name", header: "Setting", cell: (r) => r.name, rowHeader: true },
                { key: "org", header: "Organization's cap", cell: (r) => r.organization },
                { key: "platform", header: "Platform's cap", cell: (r) => r.platform },
              ]}
            />
          </Card>
          {canWrite(loaded.value.probe) && (
            <EditForm
              key={`${base}:${loaded.value.settings.version}`}
              context={context}
              base={base}
              version={loaded.value.settings.version}
              initial={organizationDraft(loaded.value.settings)}
              stored={new Set(Object.keys(loaded.value.settings.settings))}
              facts={(name) => facts(loaded.value.settings, name)}
              onSaved={(change) => {
                done(change, "Saved");
              }}
              onConflict={() => {
                setReloads((n) => n + 1);
              }}
            />
          )}
          <Card title="Applications each cap limits" label="Applications each cap limits">
            {loaded.value.applications instanceof ApiError ? (
              <p className="muted">
                {refusalText(loaded.value.applications.code, loaded.value.applications.message)}
              </p>
            ) : (
              <DataTable
                label="Applications each cap limits"
                rows={limitRows(loaded.value.settings, loaded.value.applications)}
                rowKey={(row) => row.name}
                empty="The organization has set no cap."
                columns={[
                  { key: "name", header: "Cap", cell: (r) => r.name, rowHeader: true },
                  {
                    key: "apps",
                    header: "Applications held to it now",
                    cell: (r) => (r.applications.length === 0 ? "none" : r.applications.join(", ")),
                  },
                ]}
              />
            )}
          </Card>
          <History
            versions={loaded.value.versions.versions}
            empty="No cap has been set."
            canRevert={canWrite(loaded.value.probe) && loaded.value.settings.version > 0}
            busy={busy}
            onRevert={() => {
              void revert(loaded.value.settings.version);
            }}
          />
        </>
      )}
    </div>
  );
}
