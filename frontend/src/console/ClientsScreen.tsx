import { useState } from "react";
import { routeHash } from "../shell/routes";
import { Button } from "../ui/Button";
import { StatTile, StatTiles } from "../ui/Card";
import { DataTable } from "../ui/DataTable";
import { Skeleton } from "../ui/Skeleton";
import { StatusPill, type Tone } from "../ui/StatusPill";
import { TOO_FEW, TOO_FEW_EXPLAINED, type StatusKind } from "./applications";
import { ApiError } from "./api";
import {
  bucketRows,
  CLIENT_BANNER,
  CLIENT_WINDOW_WORDS,
  CLIENT_WINDOWS,
  clientApplicationRows,
  clientRows,
  CLIENTS_PAGE_SIZE,
  healthItems,
  partial,
  PLATFORM_SETTINGS_WORDS,
  spanText,
  type ClientWindow,
} from "./clients";
import type { ConsoleContext } from "./context";
import { refusalText } from "./derive";
import { FigureText } from "./FigureText";
import type {
  ClientApplicationMetrics,
  ClientApplications,
  Health,
  OrganizationList,
  PlatformUsage,
} from "./types";
import { useLoad } from "./useLoad";

/** A status's colour; its word is always written beside it. */
const STATUS_TONES: Record<StatusKind, Tone> = {
  firing: "bad",
  attention: "warn",
  ok: "ok",
  no_requests: "idle",
  unknown: "info",
};

const ELLIPSIS = String.fromCodePoint(0x2026);

function query(window: ClientWindow, after: string | null): string {
  const page = `limit=${CLIENTS_PAGE_SIZE.toString()}`;
  return `window=${window}&${page}${after === null ? "" : `&after=${encodeURIComponent(after)}`}`;
}

/** `/readyz`: ready, not ready (503), or not known. */
async function readiness(context: ConsoleContext): Promise<boolean | null> {
  try {
    await context.api.get<unknown>("/readyz");
    return true;
  } catch (error) {
    return error instanceof ApiError && error.status === 503 ? false : null;
  }
}

async function evaluatorState(context: ConsoleContext): Promise<string | null> {
  const health = await context.api.get<Health>("/healthz").catch(() => null);
  return health?.alert_evaluator ?? null;
}

/** The window switch: last 60 minutes or last 24 hours, the one shown pressed. */
function WindowSwitch({
  window,
  onChange,
}: {
  window: ClientWindow;
  onChange: (window: ClientWindow) => void;
}) {
  return (
    <div className="window-switch" role="group" aria-label="Window">
      {CLIENT_WINDOWS.map((option) => (
        <Button
          key={option}
          size="sm"
          variant={option === window ? "primary" : "secondary"}
          aria-pressed={option === window}
          onClick={() => {
            onChange(option);
          }}
        >
          {CLIENT_WINDOW_WORDS[option]}
        </Button>
      ))}
    </div>
  );
}

function Figure({ text }: { text: string }) {
  return (
    <span className="figure">
      <FigureText text={text} />
    </span>
  );
}

interface ClientsLoaded {
  list: OrganizationList;
  usage: PlatformUsage | null;
  evaluator: string | null;
  ready: boolean | null;
}

/**
 * The platform owner's clients (docs/57): the platform's health, then one row per organization,
 * a page of 50, with its figures over the window and its open incidents. What a row may show and
 * every figure are the gateway's answers; nothing here edits a client.
 */
export function ClientsScreen({
  context,
  notice = null,
}: {
  context: ConsoleContext;
  notice?: string | null;
}) {
  const [window, setWindow] = useState<ClientWindow>("60m");
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const after = cursors[cursors.length - 1] ?? null;
  const loaded = useLoad(`clients:${window}:${after ?? ""}`, async (): Promise<ClientsLoaded> => {
    const q = query(window, after);
    const [list, usage, evaluator, ready] = await Promise.all([
      context.api.get<OrganizationList>(
        `/v1/admin/orgs?limit=${CLIENTS_PAGE_SIZE.toString()}${
          after === null ? "" : `&after=${encodeURIComponent(after)}`
        }`,
      ),
      context.api.get<PlatformUsage>(`/v1/admin/platform/usage?${q}`).catch(() => null),
      evaluatorState(context),
      readiness(context),
    ]);
    return { list, usage, evaluator, ready };
  });

  return (
    <section className="console-screen panel" aria-labelledby="clients-title">
      <h2 id="clients-title">Clients</h2>
      {notice !== null && (
        <p className="form-error" role="alert">
          {notice}
        </p>
      )}
      <p className="muted">{PLATFORM_SETTINGS_WORDS}</p>
      <WindowSwitch
        window={window}
        onChange={(next) => {
          setWindow(next);
          setCursors([null]);
        }}
      />
      {loaded.kind === "loading" && <Skeleton label={`Loading clients${ELLIPSIS}`} lines={4} />}
      {loaded.kind === "failed" && (
        <p className="form-error" role="alert">
          {refusalText(loaded.error.code, loaded.error.message)}
        </p>
      )}
      {loaded.kind === "ready" && (
        <ClientsTable
          loaded={loaded.value}
          window={window}
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

function ClientsTable({
  loaded,
  window,
  onNext,
  onFirst,
}: {
  loaded: ClientsLoaded;
  window: ClientWindow;
  onNext: (cursor: string) => void;
  onFirst: (() => void) | null;
}) {
  const span = loaded.usage?.window ?? null;
  const words = spanText(span, window);
  const rows = clientRows(loaded.list, loaded.usage, loaded.evaluator);
  const next = loaded.list.next;
  return (
    <>
      <section aria-labelledby="platform-health" className="platform-health">
        <h3 id="platform-health">Platform</h3>
        <p className={partial(span) ? "notice" : "muted"}>Totals over the {words}.</p>
        <StatTiles>
          {healthItems(loaded.evaluator, loaded.ready, loaded.usage?.totals ?? null).map((item) => (
            <StatTile
              key={item.label}
              label={item.label}
              value={
                item.tone === "info" ? (
                  <Figure text={item.value} />
                ) : (
                  <StatusPill tone={item.tone}>{item.value}</StatusPill>
                )
              }
            />
          ))}
        </StatTiles>
      </section>
      <h3>Organizations</h3>
      <p className={partial(span) ? "notice" : "muted"}>Figures over the {words}.</p>
      <DataTable
        label="Clients"
        rows={rows}
        rowKey={(row) => row.id}
        empty="No organization yet."
        columns={[
          {
            key: "name",
            header: "Name",
            rowHeader: true,
            sortValue: (row) => row.name,
            cell: (row) => (
              <>
                <a
                  href={routeHash({
                    screen: "client",
                    organizationId: row.id,
                    applicationId: null,
                  })}
                >
                  {row.name}
                </a>{" "}
                <span className="muted figure">({row.id})</span>
              </>
            ),
          },
          { key: "kind", header: "Kind", cell: (row) => row.kind },
          {
            key: "enabled",
            header: "Enabled",
            cell: (row) => (row.enabled ? "enabled" : "disabled"),
          },
          {
            key: "applications",
            header: "Applications",
            sortValue: (row) => row.applications,
            cell: (row) => <span className="figure">{row.applications}</span>,
          },
          {
            key: "requests",
            header: "Requests",
            cell: (row) => <Figure text={row.requests} />,
          },
          { key: "errors", header: "Error rate", cell: (row) => <Figure text={row.errorRate} /> },
          {
            key: "fallbacks",
            header: "Fallback rate",
            cell: (row) => <Figure text={row.fallbackRate} />,
          },
          { key: "open", header: "Open incidents", cell: (row) => row.open },
          {
            key: "status",
            header: "Health",
            sortValue: (row) => row.status.words,
            cell: (row) => (
              <StatusPill tone={STATUS_TONES[row.status.kind]}>{row.status.words}</StatusPill>
            ),
          },
        ]}
      />
      {rows.some((row) => [row.requests, row.errorRate, row.fallbackRate].includes(TOO_FEW)) && (
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

/**
 * One client, as the platform owner sees it (docs/57): metrics only, said in a banner. Its
 * applications with their figures and open incidents; a row opens that application's figures
 * over the window as a table. No link to the client's settings, rules, channels, incidents or
 * keys: acting as a member of the client is the only way to change anything there.
 */
export function ClientScreen({
  context,
  organizationId,
  applicationId,
}: {
  context: ConsoleContext;
  organizationId: string;
  applicationId: string | null;
}) {
  const [window, setWindow] = useState<ClientWindow>("60m");
  return (
    <section className="console-screen panel" aria-labelledby="client-title">
      <h2 id="client-title">
        {organizationId}
        {applicationId !== null && <> / {applicationId}</>}
      </h2>
      <p className="notice" role="note">
        {CLIENT_BANNER}
      </p>
      <WindowSwitch window={window} onChange={setWindow} />
      {applicationId === null ? (
        <ClientApplicationsView context={context} organizationId={organizationId} window={window} />
      ) : (
        <ClientApplicationView
          context={context}
          organizationId={organizationId}
          applicationId={applicationId}
          window={window}
        />
      )}
    </section>
  );
}

function clientPath(organizationId: string): string {
  return `/v1/admin/platform/orgs/${encodeURIComponent(organizationId)}/applications`;
}

function ClientApplicationsView({
  context,
  organizationId,
  window,
}: {
  context: ConsoleContext;
  organizationId: string;
  window: ClientWindow;
}) {
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const after = cursors[cursors.length - 1] ?? null;
  const loaded = useLoad(`client:${organizationId}:${window}:${after ?? ""}`, async () => {
    const [applications, evaluator] = await Promise.all([
      context.api.get<ClientApplications>(
        `${clientPath(organizationId)}/metrics?${query(window, after)}`,
      ),
      evaluatorState(context),
    ]);
    return { applications, evaluator };
  });
  if (loaded.kind === "loading") {
    return <Skeleton label={`Loading applications${ELLIPSIS}`} lines={4} />;
  }
  if (loaded.kind === "failed") {
    return (
      <p className="form-error" role="alert">
        {refusalText(loaded.error.code, loaded.error.message)}
      </p>
    );
  }
  const { applications, evaluator } = loaded.value;
  const rows = clientApplicationRows(applications.applications, evaluator);
  const span = applications.window;
  const next = applications.next;
  return (
    <>
      <p className={partial(span) ? "notice" : "muted"}>
        Figures over the {spanText(span, window)}.
      </p>
      <DataTable
        label="Applications"
        rows={rows}
        rowKey={(row) => row.id}
        empty="This client has no applications."
        columns={[
          {
            key: "name",
            header: "Name",
            rowHeader: true,
            sortValue: (row) => row.name,
            cell: (row) => (
              <>
                <a href={routeHash({ screen: "client", organizationId, applicationId: row.id })}>
                  {row.name}
                </a>{" "}
                <span className="muted figure">({row.id})</span>
              </>
            ),
          },
          { key: "requests", header: "Requests", cell: (row) => <Figure text={row.requests} /> },
          { key: "errors", header: "Error rate", cell: (row) => <Figure text={row.errorRate} /> },
          {
            key: "fallbacks",
            header: "Fallback rate",
            cell: (row) => <Figure text={row.fallbackRate} />,
          },
          {
            key: "p50",
            header: "p50 latency (to within about 19%)",
            cell: (row) => <Figure text={row.p50} />,
          },
          { key: "open", header: "Open incidents", cell: (row) => row.open },
          {
            key: "status",
            header: "Health",
            sortValue: (row) => row.status.words,
            cell: (row) => (
              <StatusPill tone={STATUS_TONES[row.status.kind]}>{row.status.words}</StatusPill>
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
        {cursors.length > 1 && (
          <Button
            onClick={() => {
              setCursors([null]);
            }}
          >
            First page
          </Button>
        )}
        {next !== null && (
          <Button
            onClick={() => {
              setCursors([...cursors, next]);
            }}
          >
            Next page
          </Button>
        )}
      </div>
    </>
  );
}

function ClientApplicationView({
  context,
  organizationId,
  applicationId,
  window,
}: {
  context: ConsoleContext;
  organizationId: string;
  applicationId: string;
  window: ClientWindow;
}) {
  const loaded = useLoad(`client-app:${organizationId}:${applicationId}:${window}`, () =>
    context.api.get<ClientApplicationMetrics>(
      `${clientPath(organizationId)}/${encodeURIComponent(applicationId)}/metrics?window=${window}`,
    ),
  );
  if (loaded.kind === "loading") {
    return <Skeleton label={`Loading figures${ELLIPSIS}`} lines={4} />;
  }
  if (loaded.kind === "failed") {
    return (
      <p className="form-error" role="alert">
        {refusalText(loaded.error.code, loaded.error.message)}
      </p>
    );
  }
  const metrics = loaded.value;
  const rows = bucketRows(metrics);
  const unit = window === "24h" ? "hour" : "minute";
  return (
    <>
      <p>
        <strong>{metrics.name}</strong>
      </p>
      <p className={partial(metrics.window) ? "notice" : "muted"}>
        Each {unit} of the {spanText(metrics.window, window)}, newest first; times in UTC.
      </p>
      <DataTable
        label={`${metrics.name}, each ${unit}`}
        rows={rows}
        rowKey={(row) => row.key}
        empty="No figures."
        columns={[
          { key: "when", header: `Starts (UTC)`, rowHeader: true, cell: (row) => row.when },
          { key: "requests", header: "Requests", cell: (row) => <Figure text={row.requests} /> },
          { key: "errors", header: "Error rate", cell: (row) => <Figure text={row.errorRate} /> },
          {
            key: "fallbacks",
            header: "Fallback rate",
            cell: (row) => <Figure text={row.fallbackRate} />,
          },
          { key: "p50", header: "p50 latency", cell: (row) => <Figure text={row.p50} /> },
        ]}
      />
      <p className="muted table-note">
        {TOO_FEW} {TOO_FEW_EXPLAINED} Each {unit} is judged alone.
      </p>
    </>
  );
}
