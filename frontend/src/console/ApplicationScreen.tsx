import { useState } from "react";
import { ApiError, applicationSettingsPath, orgPath, probeWrite, type WriteProbe } from "./api";
import { Chooser } from "./Chooser";
import type { ConsoleContext } from "./context";
import {
  canWrite,
  presetRows,
  refusalText,
  settingRows,
  versionRows,
  type PresetRow,
} from "./derive";
import { EditForm } from "./EditForm";
import { useLoad } from "./useLoad";
import type { ApplicationSettings, Change, PresetList, VersionPage } from "./types";
import { TableScroll } from "../components/TableScroll";

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
        <li key={preset.name}>
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
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                onApply(preset.name);
              }}
            >
              Apply {preset.name}
            </button>
          )}
        </li>
      ))}
    </ul>
  );
}

/** One application's settings: what is in force and why, the edit form, presets, history. */
export function ApplicationScreen({
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
  const base =
    applicationId === null
      ? null
      : applicationSettingsPath(context.user.organization_id, applicationId);
  const loaded = useLoad(`settings:${base ?? ""}:${reloads}`, () =>
    base === null ? Promise.reject(new ApiError(0, "none", "")) : loadAll(context, base),
  );

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
      <h2>Application settings</h2>
      <Chooser
        context={context}
        selected={applicationId}
        onChoose={(id) => {
          if (id !== null) onChoose(id);
        }}
      />
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
      {base !== null && loaded.kind === "loading" && <p className="muted">Loading…</p>}
      {base !== null && loaded.kind === "failed" && (
        <p className="form-error" role="alert">
          {refusalText(loaded.error.code, loaded.error.message)}
        </p>
      )}
      {base !== null && loaded.kind === "ready" && (
        <>
          <dl className="facts">
            <div>
              <dt>Version</dt>
              <dd>{loaded.value.settings.version}</dd>
            </div>
            <div>
              <dt>Mode in force</dt>
              <dd>{loaded.value.settings.mode.effective}</dd>
            </div>
            <div>
              <dt>Access</dt>
              <dd>{canWrite(loaded.value.probe) ? "May change" : "Read only"}</dd>
            </div>
          </dl>
          <section className="panel" aria-labelledby="in-force-title">
            <h3 id="in-force-title">Settings in force</h3>
            <TableScroll table="Settings in force">
              <table className="data-table" aria-label="Settings in force">
                <thead>
                  <tr>
                    <th scope="col">Setting</th>
                    <th scope="col">In force</th>
                    <th scope="col">Source</th>
                    <th scope="col">Capped by</th>
                    <th scope="col">Stored</th>
                    <th scope="col">Caps</th>
                  </tr>
                </thead>
                <tbody>
                  {settingRows(loaded.value.settings).map((row) => (
                    <tr key={row.name}>
                      <th scope="row">{row.name}</th>
                      <td>{row.value}</td>
                      <td>{row.source}</td>
                      <td>{row.cappedBy}</td>
                      <td>{row.stored}</td>
                      <td>{row.caps}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableScroll>
          </section>
          {canWrite(loaded.value.probe) && (
            <EditForm
              key={`${base}:${loaded.value.settings.version}`}
              context={context}
              base={base}
              settings={loaded.value.settings}
              onSaved={(change) => {
                done(change, "Saved");
              }}
              onConflict={() => {
                setReloads((n) => n + 1);
              }}
            />
          )}
          <section className="panel" aria-labelledby="presets-title">
            <h3 id="presets-title">Presets</h3>
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
          </section>
          <section className="panel" aria-labelledby="history-title">
            <h3 id="history-title">History</h3>
            {loaded.value.versions.versions.length === 0 ? (
              <p className="muted">Never changed: every value is inherited.</p>
            ) : (
              <TableScroll table="History">
                <table className="data-table" aria-label="History">
                  <thead>
                    <tr>
                      <th scope="col">Version</th>
                      <th scope="col">Fields changed</th>
                      <th scope="col">By</th>
                      <th scope="col">How</th>
                      <th scope="col">When</th>
                    </tr>
                  </thead>
                  <tbody>
                    {versionRows(loaded.value.versions.versions).map((row) => (
                      <tr key={row.version}>
                        <td>{row.version}</td>
                        <td>{row.fields}</td>
                        <td>{row.by}</td>
                        <td>{row.via}</td>
                        <td>{row.when}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </TableScroll>
            )}
            {canWrite(loaded.value.probe) && loaded.value.settings.version > 0 && (
              <div className="form-actions">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => {
                    void act("Reverted", `${base}/revert`, loaded.value.settings.version);
                  }}
                >
                  Revert to the previous version
                </button>
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}
