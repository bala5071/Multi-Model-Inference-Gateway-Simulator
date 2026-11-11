import { useState } from "react";
import { ApiError, applicationSettingsPath, orgPath, probeWrite, type WriteProbe } from "./api";
import type { ConsoleContext } from "./context";
import {
  canWrite,
  formatCaps,
  initialDraft,
  presetRows,
  refusalText,
  settingRows,
  type PresetRow,
} from "./derive";
import { EditForm } from "./EditForm";
import { History } from "./History";
import { displayValue } from "./settingsForm";
import { useLoad } from "./useLoad";
import type { ApplicationSettings, Change, PresetList, VersionPage } from "./types";
import { Button } from "../ui/Button";
import { Card, StatTile, StatTiles } from "../ui/Card";
import { DataTable } from "../ui/DataTable";
import { Skeleton } from "../ui/Skeleton";
import { Toasted } from "../ui/Toasts";

interface Loaded {
  settings: ApplicationSettings;
  versions: VersionPage;
  presets: PresetList | ApiError;
  probe: WriteProbe;
}

async function loadAll(context: ConsoleContext, base: string): Promise<Loaded> {
  const settings = await context.api.get<ApplicationSettings>(base);
  const [versions, presets, probe] = await Promise.all([
    context.api.get<VersionPage>(`${base}/versions?limit=20`),
    context.api
      .get<PresetList>(`${orgPath(context.user.organization_id)}/settings/presets`)
      .catch((error: unknown) =>
        error instanceof ApiError ? error : new ApiError(0, "unexpected", "Not read."),
      ),
    probeWrite(context.api, base, settings.version),
  ]);
  return { settings, versions, presets, probe };
}

/** The presets as cards: what each sets, and Apply where the gateway said this account may. */
function Presets({
  rows,
  writable,
  busy,
  onApply,
}: {
  rows: PresetRow[];
  writable: boolean;
  busy: boolean;
  onApply: (name: string) => void;
}) {
  return (
    <ul className="presets">
      {rows.map((preset) => (
        <li key={preset.name} className="card">
          <h4>{preset.name}</h4>
          <dl className="facts">
            {preset.values.map(([setting, value]) => (
              <div key={setting}>
                <dt>{setting}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
          {writable && (
            <Button
              disabled={busy}
              onClick={() => {
                onApply(preset.name);
              }}
            >
              Apply {preset.name}
            </Button>
          )}
        </li>
      ))}
    </ul>
  );
}

/** Beside each setting in the form: its value in force, where it came from, and its caps. */
function facts(settings: ApplicationSettings, name: string) {
  const found = settings.effective[name];
  if (found === undefined) return null;
  return (
    <>
      <span>In force: {displayValue(name, found.value)}</span>
      <span>Source: {found.source}</span>
      {found.capped_by !== null && <span>Capped by: {found.capped_by}</span>}
      <span>Caps: {formatCaps(found.caps)}</span>
    </>
  );
}

/**
 * One application's settings, the Settings tab of its page (docs/56): what is in force and why,
 * the edit form, presets, history.
 */
export function ApplicationScreen({
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
  const base = applicationSettingsPath(context.user.organization_id, applicationId);
  const loaded = useLoad(`settings:${base}:${reloads}`, () => loadAll(context, base));

  function done(change: Change, what: string) {
    setMessage(`${what} as version ${change.version}.`);
    setError(null);
    setBusy(false);
    setReloads((n) => n + 1);
  }

  async function act(what: string, path: string, version: number) {
    setBusy(true);
    setMessage(null);
    setError(null);
    try {
      const change = await context.guarded(() =>
        context.api.send<Change>("POST", path, { expected_version: version }),
      );
      done(change, what);
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
            <StatTile label="Version" value={loaded.value.settings.version} />
            <StatTile label="Mode in force" value={loaded.value.settings.mode.effective} />
            <StatTile
              label="Access"
              value={canWrite(loaded.value.probe) ? "May change" : "Read only"}
            />
          </StatTiles>
          <Card title="Settings in force" label="Settings in force">
            <DataTable
              label="Settings in force"
              rows={settingRows(loaded.value.settings)}
              rowKey={(row) => row.name}
              empty="No setting."
              columns={[
                { key: "name", header: "Setting", cell: (r) => r.name, rowHeader: true },
                { key: "value", header: "In force", cell: (r) => r.value },
                { key: "source", header: "Source", cell: (r) => r.source },
                { key: "capped", header: "Capped by", cell: (r) => r.cappedBy },
                { key: "stored", header: "Stored", cell: (r) => r.stored },
                { key: "caps", header: "Caps", cell: (r) => r.caps },
              ]}
            />
          </Card>
          {canWrite(loaded.value.probe) && (
            <EditForm
              key={`${base}:${loaded.value.settings.version}`}
              context={context}
              base={base}
              version={loaded.value.settings.version}
              initial={initialDraft(loaded.value.settings)}
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
          <Card title="Presets" label="Presets">
            {loaded.value.presets instanceof ApiError ? (
              <p className="muted">
                {refusalText(loaded.value.presets.code, loaded.value.presets.message)}
              </p>
            ) : (
              <Presets
                rows={presetRows(loaded.value.presets.presets)}
                writable={canWrite(loaded.value.probe)}
                busy={busy}
                onApply={(name) => {
                  void act(
                    `Applied ${name}`,
                    `${base}/presets/${encodeURIComponent(name)}`,
                    loaded.value.settings.version,
                  );
                }}
              />
            )}
          </Card>
          <History
            versions={loaded.value.versions.versions}
            empty="Never changed: every value is inherited."
            canRevert={canWrite(loaded.value.probe) && loaded.value.settings.version > 0}
            busy={busy}
            onRevert={() => {
              void act("Reverted", `${base}/revert`, loaded.value.settings.version);
            }}
          />
        </>
      )}
    </div>
  );
}
