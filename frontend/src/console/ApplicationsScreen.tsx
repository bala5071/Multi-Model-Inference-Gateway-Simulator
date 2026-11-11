import { useState } from "react";
import { allows, CREATE_APPLICATION } from "../shell/navigation";
import { routeHash } from "../shell/routes";
import {
  applicationRows,
  filterRows,
  FILTER_NOTE,
  TOO_FEW,
  TOO_FEW_EXPLAINED,
  windowText,
  WINDOW_WORDS,
  type RowFilter,
  type StatusKind,
} from "./applications";
import { orgPath } from "./api";
import type { ConsoleContext } from "./context";
import { refusalText } from "./derive";
import { EvaluatorBanner } from "./EvaluatorBanner";
import { useLoad } from "./useLoad";
import type { ApplicationPage, FiguresPage, Health, OpenCountsAnswer } from "./types";
import { FigureText } from "./FigureText";
import { Button } from "../ui/Button";
import { DataTable } from "../ui/DataTable";
import { SelectField, TextField } from "../ui/Field";
import { Skeleton } from "../ui/Skeleton";
import { StatusPill, type Tone } from "../ui/StatusPill";

const STATUS_KINDS: readonly StatusKind[] = ["firing", "attention", "ok", "no_requests", "unknown"];
const STATUS_LABELS: Record<StatusKind, string> = {
  firing: "firing",
  attention: "attention",
  ok: "ok",
  no_requests: "no requests",
  unknown: "unknown",
};
/** A status's colour; its word is always written beside it. */
const STATUS_TONES: Record<StatusKind, Tone> = {
  firing: "bad",
  attention: "warn",
  ok: "ok",
  no_requests: "idle",
  unknown: "info",
};

/** The page size of the list and of its figures: the same, so they name the same applications. */
export const PAGE_SIZE = 50;

const LOADING = `Loading applications${String.fromCodePoint(0x2026)}`;

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
          <a className="button primary" href={routeHash({ screen: "apps-new" })}>
            Add application
          </a>
        </div>
      )}
      {loaded.kind === "loading" && <Skeleton label={LOADING} lines={4} />}
      {loaded.kind === "failed" && (
        <p className="form-error" role="alert">
          {refusalText(loaded.error.code, loaded.error.message)}
        </p>
      )}
      {loaded.kind === "ready" && (
        <ApplicationTable
          loaded={loaded.value}
          context={context}
          mayCreate={mayCreate}
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
  mayCreate,
  onNext,
  onFirst,
}: {
  loaded: Loaded;
  context: ConsoleContext;
  mayCreate: boolean;
  onNext: (cursor: string) => void;
  onFirst: (() => void) | null;
}) {
  const [filter, setFilter] = useState<RowFilter>({ text: "", mode: "", status: "" });
  const rows = applicationRows(
    loaded.page,
    loaded.figures,
    loaded.counts,
    loaded.evaluator,
    context.capabilities,
  );
  const span = windowText(loaded.figures?.window ?? null);
  if (rows.length === 0 && onFirst === null) {
    return (
      <div className="empty-state">
        <h3>No applications yet</h3>
        <p>
          An application is one client of the gateway: it has its own API keys, a copy of a plan
          template&apos;s models and limits, its settings, alert rules and incidents, and its own
          figures.
        </p>
        {mayCreate ? (
          <p>Add one to issue its first key.</p>
        ) : (
          <p className="muted">An administrator of this organization can add one.</p>
        )}
      </div>
    );
  }
  const shown = filterRows(rows, filter);
  const modes = [...new Set(rows.map((row) => row.mode))];
  const next = loaded.page.next;
  const open = (id: string, tab: "overview" | "keys") =>
    routeHash({ screen: "app", applicationId: id, tab, incidentId: null });
  return (
    <>
      <div className="list-filters" role="search" aria-label="Applications on this page">
        <TextField
          label="Search"
          type="search"
          value={filter.text}
          placeholder="Name or id"
          onChange={(event) => {
            setFilter({ ...filter, text: event.target.value });
          }}
        />
        <SelectField
          label="Mode"
          value={filter.mode}
          onChange={(event) => {
            setFilter({ ...filter, mode: event.target.value });
          }}
        >
          <option value="">Any mode</option>
          {modes.map((mode) => (
            <option key={mode} value={mode}>
              {mode}
            </option>
          ))}
        </SelectField>
        <SelectField
          label="Status"
          value={filter.status}
          onChange={(event) => {
            setFilter({ ...filter, status: event.target.value as RowFilter["status"] });
          }}
        >
          <option value="">Any status</option>
          {STATUS_KINDS.map((kind) => (
            <option key={kind} value={kind}>
              {STATUS_LABELS[kind]}
            </option>
          ))}
        </SelectField>
      </div>
      <p className="muted">
        {FILTER_NOTE} Figures over the {span}.{" "}
        <span role="status">
          Showing {shown.length} of {rows.length}.
        </span>
      </p>
      <DataTable
        label="Applications"
        rows={shown}
        rowKey={(row) => row.id}
        empty="No application on this page matches the search and filters."
        columns={[
          {
            key: "name",
            header: "Name",
            rowHeader: true,
            sortValue: (row) => row.name,
            cell: (row) => (
              <>
                <a href={open(row.id, "overview")}>{row.name}</a>{" "}
                <span className="muted figure">({row.id})</span>
              </>
            ),
          },
          { key: "mode", header: "Mode", cell: (row) => row.mode, sortValue: (row) => row.mode },
          {
            key: "requests",
            header: `Requests (${WINDOW_WORDS})`,
            cell: (row) => (
              <span className="figure">
                <FigureText text={row.requests} />
              </span>
            ),
          },
          {
            key: "errors",
            header: "Error rate",
            cell: (row) => (
              <span className="figure">
                <FigureText text={row.errorRate} />
              </span>
            ),
          },
          {
            key: "p50",
            header: "p50 latency (to within about 19%)",
            cell: (row) => (
              <span className="figure">
                <FigureText text={row.p50} />
              </span>
            ),
          },
          { key: "open", header: "Open incidents", cell: (row) => row.open },
          {
            key: "status",
            header: "Status",
            sortValue: (row) => row.status.words,
            cell: (row) => (
              <StatusPill tone={STATUS_TONES[row.status.kind]}>{row.status.words}</StatusPill>
            ),
          },
          {
            key: "keys",
            header: "Keys",
            cell: (row) =>
              row.keys ? (
                <a href={open(row.id, "keys")}>API keys</a>
              ) : (
                <span className="muted">-</span>
              ),
          },
        ]}
      />
      {rows.some((row) => [row.requests, row.errorRate, row.p50].includes(TOO_FEW)) && (
        <p className="muted table-note">
          {TOO_FEW} {TOO_FEW_EXPLAINED}
        </p>
      )}
      <div className="form-actions">
        {onFirst !== null && <Button onClick={onFirst}>First page</Button>}
        {next !== null && (
          <Button
            onClick={() => {
              onNext(next);
            }}
          >
            Next page
          </Button>
        )}
      </div>
    </>
  );
}
