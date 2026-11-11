import { useState } from "react";
import { allows, CREATE_APPLICATION } from "../shell/navigation";
import { routeHash } from "../shell/routes";
import { applicationRows, windowText, WINDOW_WORDS } from "./applications";
import { orgPath } from "./api";
import type { ConsoleContext } from "./context";
import { refusalText } from "./derive";
import { EvaluatorBanner } from "./EvaluatorBanner";
import { useLoad } from "./useLoad";
import type { ApplicationPage, FiguresPage, Health, OpenCountsAnswer } from "./types";
import { TableScroll } from "../components/TableScroll";

/** The page size of the list and of its figures: the same, so they name the same applications. */
export const PAGE_SIZE = 50;

interface Loaded {
  page: ApplicationPage;
  figures: FiguresPage | null;
  counts: OpenCountsAnswer | null;
  evaluator: string | null;
}

/** The list's page, and beside it what can be read for it; each of those may be refused. */
async function load(context: ConsoleContext, after: string | null): Promise<Loaded> {
  const base = orgPath(context.user.organization_id);
  const query = `limit=${PAGE_SIZE.toString()}${after === null ? "" : `&after=${encodeURIComponent(after)}`}`;
  const [page, figures, counts, health] = await Promise.all([
    context.api.get<ApplicationPage>(`${base}/applications?${query}`),
    context.api.get<FiguresPage>(`${base}/metrics?window=60m&${query}`).catch(() => null),
    context.api.get<OpenCountsAnswer>(`${base}/incidents/counts`).catch(() => null),
    context.api.get<Health>("/healthz").catch(() => null),
  ]);
  return { page, figures, counts, evaluator: health?.alert_evaluator ?? null };
}

/**
 * The organization's applications (docs/51): each with its mode, its figures over the last 60
 * minutes, its open incidents and its status, a page at a time. "Add application" only when the
 * gateway's capabilities answer allows creating one; nothing here is decided by the page.
 */
export function ApplicationsScreen({
  context,
  notice = null,
}: {
  context: ConsoleContext;
  notice?: string | null;
}) {
  // The cursors of the pages before this one, for going back: the first page has none.
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const after = cursors[cursors.length - 1] ?? null;
  const org = context.user.organization_id;
  const loaded = useLoad(`applications:${org}:${after ?? ""}`, () => load(context, after));
  const mayCreate = allows(context.capabilities, CREATE_APPLICATION);

  return (
    <section className="console-screen panel" aria-labelledby="applications-title">
      <h2 id="applications-title">Applications</h2>
      {notice !== null && (
        <p className="form-error" role="alert">
          {notice}
        </p>
      )}
      <EvaluatorBanner api={context.api} />
      {mayCreate && (
        <div className="form-actions">
          <a className="button" href={routeHash({ screen: "apps-new" })}>
            Add application
          </a>
        </div>
      )}
      {loaded.kind === "loading" && <p className="muted">Loading applications…</p>}
      {loaded.kind === "failed" && (
        <p className="form-error" role="alert">
          {refusalText(loaded.error.code, loaded.error.message)}
        </p>
      )}
      {loaded.kind === "ready" && (
        <ApplicationTable
          loaded={loaded.value}
          context={context}
          onNext={(next) => {
            setCursors([...cursors, next]);
          }}
          onFirst={
            cursors.length > 1
              ? () => {
                  setCursors([null]);
                }
              : null
          }
        />
      )}
    </section>
  );
}

function ApplicationTable({
  loaded,
  context,
  onNext,
  onFirst,
}: {
  loaded: Loaded;
  context: ConsoleContext;
  onNext: (cursor: string) => void;
  onFirst: (() => void) | null;
}) {
  const rows = applicationRows(
    loaded.page,
    loaded.figures,
    loaded.counts,
    loaded.evaluator,
    context.capabilities,
  );
  const span = windowText(loaded.figures?.window ?? null);
  if (rows.length === 0) {
    return <p className="muted">This organization has no applications yet.</p>;
  }
  const next = loaded.page.next;
  return (
    <>
      <p className="muted">Figures over the {span}.</p>
      <TableScroll table="Applications">
        <table className="data-table" aria-label="Applications">
          <thead>
            <tr>
              <th scope="col">Name</th>
              <th scope="col">Mode</th>
              <th scope="col">Requests ({WINDOW_WORDS})</th>
              <th scope="col">Error rate</th>
              <th scope="col">p50 latency (to within about 19%)</th>
              <th scope="col">Open incidents</th>
              <th scope="col">Status</th>
              <th scope="col">Keys</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id}>
                <th scope="row">
                  <a href={routeHash({ screen: "application", applicationId: row.id })}>
                    {row.name}
                  </a>{" "}
                  <span className="muted figure">({row.id})</span>
                </th>
                <td>{row.mode}</td>
                <td className="figure">{row.requests}</td>
                <td className="figure">{row.errorRate}</td>
                <td className="figure">{row.p50}</td>
                <td>{row.open}</td>
                <td>
                  <span className={`status-dot status-${row.status.kind}`} aria-hidden="true" />{" "}
                  {row.status.words}
                </td>
                <td>
                  {row.keys ? (
                    <a href={routeHash({ screen: "keys", applicationId: row.id })}>API keys</a>
                  ) : (
                    <span className="muted">-</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </TableScroll>
      <div className="form-actions">
        {onFirst !== null && (
          <button type="button" onClick={onFirst}>
            First page
          </button>
        )}
        {next !== null && (
          <button
            type="button"
            onClick={() => {
              onNext(next);
            }}
          >
            Next page
          </button>
        )}
      </div>
    </>
  );
}
