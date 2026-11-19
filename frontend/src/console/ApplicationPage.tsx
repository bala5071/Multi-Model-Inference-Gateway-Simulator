import { useEffect } from "react";
import { TAB_ICONS } from "../shell/icons";
import { allows } from "../shell/navigation";
import { APP_TAB_WORDS, routeHash, type AppTab, type Route } from "../shell/routes";
import { applicationPath, orgPath } from "./api";
import { ApplicationScreen } from "./ApplicationScreen";
import { figureText, modeText, windowText, WINDOW_WORDS } from "./applications";
import type { ConsoleContext } from "./context";
import { refusalText } from "./derive";
import { FigureText } from "./FigureText";
import { OPEN_STATES } from "../shell/Home";
import { ApplicationIncidents } from "./IncidentsScreen";
import { KeysScreen } from "./KeysScreen";
import { RulesScreen } from "./RulesScreen";
import { useLoad } from "./useLoad";
import type { ApplicationFigures, ApplicationSummary, FiguresPage, IncidentPage } from "./types";
import { Card, StatTile, StatTiles } from "../ui/Card";
import { Skeleton } from "../ui/Skeleton";

/** Who may read an application's figures today: the organization's usage (docs/51, docs/56). */
export const ORGANIZATION_USAGE = {
  level: "organization",
  action: "usage.read_organization",
} as const;

/** What the Overview says where the gateway gives this session no figures (docs/52, 17.7). */
export const NO_FIGURES =
  "Figures for this application are not available to this account yet: the gateway gives them to the organization's administrators and readers only.";

/** How many pages of the organization's figures the Overview reads to find this application. */
const FIGURE_PAGES = 20;

/** This application's row of the organization's figures, read a page at a time until found. */
async function figuresOf(
  context: ConsoleContext,
  applicationId: string,
): Promise<{ row: ApplicationFigures | null; page: FiguresPage }> {
  const base = `${orgPath(context.user.organization_id)}/metrics?window=60m&limit=100`;
  let after: string | null = null;
  let page: FiguresPage | null = null;
  for (let read = 0; read < FIGURE_PAGES; read += 1) {
    const query: string = after === null ? "" : `&after=${encodeURIComponent(after)}`;
    page = await context.api.get<FiguresPage>(`${base}${query}`);
    const row = page.applications.find((a) => a.application_id === applicationId);
    if (row !== undefined) return { row, page };
    if (page.next === null) break;
    after = page.next;
  }
  if (page === null) throw new Error("No page was read.");
  return { row: null, page };
}

/** How many of this application's incidents are open, one read per open state. */
async function openOf(context: ConsoleContext, applicationId: string) {
  const app = applicationPath(context.user.organization_id, applicationId);
  const pages = await Promise.all(
    OPEN_STATES.map((state) =>
      context.api.get<IncidentPage>(`${app}/incidents?state=${state}&limit=100`),
    ),
  );
  return OPEN_STATES.map((state, i) => {
    const page = pages[i];
    const count = page?.incidents.length ?? 0;
    return { state, count, more: page?.next !== null && page?.next !== undefined };
  });
}

/**
 * The Overview (docs/52, 17.7): what the gateway returns for this application today and nothing
 * more, each figure with what it covers. Its plan and mode; its open incidents; and, for a session
 * the gateway lets read the organization's usage, its figures over the last 60 minutes. No chart:
 * the gateway returns no series for one application yet.
 */
function Overview({
  context,
  applicationId,
  summary,
}: {
  context: ConsoleContext;
  applicationId: string;
  summary: ApplicationSummary;
}) {
  const mayReadFigures = allows(context.capabilities, ORGANIZATION_USAGE);
  const open = useLoad(`open:${applicationId}`, () => openOf(context, applicationId));
  const figures = useLoad(`figures:${applicationId}:${String(mayReadFigures)}`, () =>
    mayReadFigures ? figuresOf(context, applicationId) : Promise.resolve(null),
  );
  return (
    <div className="console-screen">
      <Card title="Application" label="Application">
        <StatTiles>
          <StatTile label="Plan" value={summary.plan} />
          <StatTile label="Mode in force" value={modeText(summary.mode)} />
          <StatTile
            label="Models"
            value={summary.allowed_models.length === 0 ? "none" : summary.allowed_models.join(", ")}
          />
          <StatTile
            label="Requests a minute (limit)"
            value={summary.requests_per_minute ?? "not set"}
          />
        </StatTiles>
      </Card>
      <Card title="Open incidents" label="Open incidents">
        {open.kind === "loading" && <Skeleton lines={1} />}
        {open.kind === "failed" && (
          <p className="muted">{refusalText(open.error.code, open.error.message)}</p>
        )}
        {open.kind === "ready" && (
          <StatTiles>
            {open.value.map(({ state, count, more }) => (
              <StatTile
                key={state}
                label={state}
                value={more ? `${String(count)} or more` : count}
              />
            ))}
          </StatTiles>
        )}
      </Card>
      <Card title={`Figures (${WINDOW_WORDS})`} label="Figures">
        {!mayReadFigures && <p className="muted">{NO_FIGURES}</p>}
        {mayReadFigures && figures.kind === "loading" && <Skeleton lines={1} />}
        {mayReadFigures && figures.kind === "failed" && (
          <p className="muted">{refusalText(figures.error.code, figures.error.message)}</p>
        )}
        {mayReadFigures && figures.kind === "ready" && figures.value !== null && (
          <>
            <p className="muted">
              Figures over the {windowText(figures.value.page.window)}, from the gateway&apos;s
              alert rollups: single figures for the window, not a series.
            </p>
            {figures.value.row === null ? (
              <p className="muted">
                The organization&apos;s figures did not name this application.
              </p>
            ) : (
              <StatTiles>
                <StatTile
                  label={`Requests (${WINDOW_WORDS})`}
                  value={<FigureText text={figureText(figures.value.row.requests, "count")} />}
                />
                <StatTile
                  label="Error rate"
                  value={<FigureText text={figureText(figures.value.row.error_rate, "percent")} />}
                />
                <StatTile
                  label="p50 latency (to within about 19%)"
                  value={
                    <FigureText text={figureText(figures.value.row.latency_total_p50, "ms")} />
                  }
                />
              </StatTiles>
            )}
          </>
        )}
      </Card>
    </div>
  );
}

/**
 * An application's page (console-ia-v2-plan 2.4, docs/56): its name, then its tabs, each one the
 * gateway allows this session (`tabs`); the tabs are links, so each is an address of its own and
 * the browser's back goes to the one before.
 */
export function ApplicationPage({
  context,
  applicationId,
  tab,
  incidentId,
  tabs,
  onRoute,
  onName,
  notice = null,
}: {
  context: ConsoleContext;
  applicationId: string;
  tab: AppTab;
  incidentId: string | null;
  tabs: readonly AppTab[];
  onRoute: (route: Route) => void;
  onName: (name: string | null) => void;
  /** Said at the top: "not available", when the session was brought here from elsewhere. */
  notice?: string | null;
}) {
  const summary = useLoad(`application:${applicationId}`, () =>
    context.api.get<ApplicationSummary>(
      applicationPath(context.user.organization_id, applicationId),
    ),
  );
  const name = summary.kind === "ready" ? summary.value.name : null;
  useEffect(() => {
    onName(name);
    return () => {
      onName(null);
    };
  }, [name, onName]);

  return (
    <section className="console-screen app-page" aria-labelledby="app-title">
      <div className="app-head">
        <h2 id="app-title">{name ?? applicationId}</h2>
        <span className="muted figure">{applicationId}</span>
      </div>
      {notice !== null && (
        <p className="form-error" role="alert">
          {notice}
        </p>
      )}
      <nav className="app-tabs" aria-label="Application sections">
        <ul>
          {tabs.map((each) => {
            const Icon = TAB_ICONS[each];
            return (
              <li key={each}>
                <a
                  href={routeHash({ screen: "app", applicationId, tab: each, incidentId: null })}
                  aria-current={each === tab ? "page" : undefined}
                >
                  <Icon aria-hidden="true" focusable="false" size={16} />
                  {APP_TAB_WORDS[each]}
                </a>
              </li>
            );
          })}
        </ul>
      </nav>
      {tab === "overview" &&
        (summary.kind === "loading" ? (
          <Skeleton />
        ) : summary.kind === "failed" ? (
          <p className="form-error" role="alert">
            {refusalText(summary.error.code, summary.error.message)}
          </p>
        ) : (
          <Overview context={context} applicationId={applicationId} summary={summary.value} />
        ))}
      {tab === "settings" && <ApplicationScreen context={context} applicationId={applicationId} />}
      {tab === "rules" && <RulesScreen context={context} applicationId={applicationId} />}
      {tab === "incidents" && (
        <ApplicationIncidents
          context={context}
          applicationId={applicationId}
          incidentId={incidentId}
          onRoute={(id) => {
            onRoute({ screen: "app", applicationId, tab: "incidents", incidentId: id });
          }}
        />
      )}
      {tab === "keys" && <KeysScreen context={context} applicationId={applicationId} />}
    </section>
  );
}
