import { ApiError, applicationSettingsPath, orgPath } from "./api";
import type { ConsoleContext } from "./context";
import { capRows, limitRows, refusalText, versionRows } from "./derive";
import { useLoad } from "./useLoad";
import type {
  ApplicationPage,
  ApplicationSettings,
  ApplicationSummary,
  OrganizationSettings,
  VersionPage,
} from "./types";

interface Loaded {
  settings: OrganizationSettings;
  versions: VersionPage;
  applications: { summary: ApplicationSummary; settings: ApplicationSettings }[] | ApiError;
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
  const [versions, applications] = await Promise.all([
    context.api.get<VersionPage>(`${base}/versions?limit=20`),
    applicationsOf(context).catch((error: unknown) =>
      error instanceof ApiError ? error : new ApiError(0, "unexpected", "Not read."),
    ),
  ]);
  return { settings, versions, applications };
}

/**
 * The organization's caps, the applications each one holds down now, and their history. It
 * shows; it changes nothing (C1 of the settings and alerts plan).
 */
export function OrganizationScreen({ context }: { context: ConsoleContext }) {
  const loaded = useLoad(`organization:${context.user.organization_id}`, () => loadAll(context));

  return (
    <div className="console-screen">
      <h2>Organization settings</h2>
      {loaded.kind === "loading" && <p className="muted">Loading…</p>}
      {loaded.kind === "failed" && (
        <p className="form-error" role="alert">
          {refusalText(loaded.error.code, loaded.error.message)}
        </p>
      )}
      {loaded.kind === "ready" && (
        <>
          <dl className="facts">
            <div>
              <dt>Organization</dt>
              <dd>{loaded.value.settings.organization_id}</dd>
            </div>
            <div>
              <dt>Version</dt>
              <dd>{loaded.value.settings.version}</dd>
            </div>
            <div>
              <dt>Mode</dt>
              <dd>{loaded.value.settings.mode}</dd>
            </div>
          </dl>
          <section className="panel" aria-labelledby="caps-title">
            <h3 id="caps-title">Caps</h3>
            <div className="table-scroll">
              <table className="data-table">
                <thead>
                  <tr>
                    <th scope="col">Setting</th>
                    <th scope="col">Organization&apos;s cap</th>
                    <th scope="col">Platform&apos;s cap</th>
                  </tr>
                </thead>
                <tbody>
                  {capRows(loaded.value.settings).map((row) => (
                    <tr key={row.name}>
                      <th scope="row">{row.name}</th>
                      <td>{row.organization}</td>
                      <td>{row.platform}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
          <section className="panel" aria-labelledby="limits-title">
            <h3 id="limits-title">Applications each cap limits</h3>
            {loaded.value.applications instanceof ApiError ? (
              <p className="muted">
                {refusalText(loaded.value.applications.code, loaded.value.applications.message)}
              </p>
            ) : limitRows(loaded.value.settings, loaded.value.applications).length === 0 ? (
              <p className="muted">The organization has set no cap.</p>
            ) : (
              <div className="table-scroll">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th scope="col">Cap</th>
                      <th scope="col">Applications held to it now</th>
                    </tr>
                  </thead>
                  <tbody>
                    {limitRows(loaded.value.settings, loaded.value.applications).map((row) => (
                      <tr key={row.name}>
                        <th scope="row">{row.name}</th>
                        <td>
                          {row.applications.length === 0 ? "none" : row.applications.join(", ")}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
          <section className="panel" aria-labelledby="org-history-title">
            <h3 id="org-history-title">History</h3>
            {loaded.value.versions.versions.length === 0 ? (
              <p className="muted">No cap has been set.</p>
            ) : (
              <div className="table-scroll">
                <table className="data-table">
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
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}
