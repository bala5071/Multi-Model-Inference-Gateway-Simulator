import { orgPath } from "../console/api";
import type { ConsoleContext } from "../console/context";
import { EvaluatorBanner } from "../console/EvaluatorBanner";
import type { Capabilities, IncidentPage } from "../console/types";
import { useLoad } from "../console/useLoad";
import {
  allows,
  appTabs,
  isShown,
  NAVIGATION,
  ORGANIZATION_INCIDENTS,
  target,
  type ItemId,
} from "./navigation";
import { APP_TAB_WORDS, routeHash, type Route } from "./routes";

/** The incident states counted as open (`OPEN_STATES` in the gateway's incidents.py). */
export const OPEN_STATES = ["pending", "firing", "acknowledged"] as const;
const PAGE = 100;

function any(answers: Record<string, boolean>): boolean {
  return Object.values(answers).some((allowed) => allowed);
}

/** A link to an item if the answer shows it, from home, with its label. */
function ItemLink({
  id,
  capabilities,
  from = { screen: "home" },
}: {
  id: ItemId;
  capabilities: Capabilities;
  from?: Route;
}) {
  const item = NAVIGATION.find((candidate) => candidate.id === id);
  if (item === undefined || !isShown(item, capabilities)) return null;
  return (
    <li>
      <a href={routeHash(target(item, from))}>{item.label}</a>
    </li>
  );
}

/** How many of the organization's incidents are open: one read per open state. */
function OpenIncidents({ context }: { context: ConsoleContext }) {
  const org = context.user.organization_id;
  const counted = useLoad(`open:${org}`, async () => {
    const pages = await Promise.all(
      OPEN_STATES.map((state) =>
        context.api.get<IncidentPage>(`${orgPath(org)}/incidents?state=${state}&limit=${PAGE}`),
      ),
    );
    return {
      count: pages.reduce((total, page) => total + page.incidents.length, 0),
      more: pages.some((page) => page.next !== null),
    };
  });
  if (counted.kind === "loading") return <p className="muted">Counting open incidents…</p>;
  if (counted.kind === "failed") return <p className="muted">Open incidents could not be read.</p>;
  const { count, more } = counted.value;
  return (
    <p>
      Open incidents: <strong>{more ? `${count} or more` : count}</strong>
    </p>
  );
}

/**
 * The home screen (docs/50), drawn from what the gateway said this session may do, never from a
 * role's name: a section for the platform, one for the organization, the applications for a
 * session that has only those, or a sentence saying there is nothing yet.
 */
export function Home({
  context,
  notice,
  onLogout,
}: {
  context: ConsoleContext;
  notice: string | null;
  onLogout: () => void;
}) {
  const { capabilities, user } = context;
  const platform = any(capabilities.platform);
  const organization = any(capabilities.organization);
  const applicationsOnly = !organization && capabilities.applications.length > 0;
  const nothing = !platform && !organization && capabilities.applications.length === 0;
  return (
    <section className="panel home" aria-labelledby="home-title">
      <h2 id="home-title">Home</h2>
      {notice !== null && (
        <p className="form-error" role="alert">
          {notice}
        </p>
      )}
      {platform && (
        <section aria-labelledby="home-platform">
          <h3 id="home-platform">Platform</h3>
          <ul>
            <ItemLink id="monitor" capabilities={capabilities} />
          </ul>
          <p>
            Organizations and their users are managed with <code>scripts/manage.py</code>: there is
            no screen for them yet.
          </p>
          <p className="muted">
            Each organization&apos;s settings, alert rules, channels and incidents are managed by
            that organization&apos;s own administrators.
          </p>
        </section>
      )}
      {organization && (
        <section aria-labelledby="home-organization">
          <h3 id="home-organization">Organization {user.organization_id}</h3>
          <EvaluatorBanner api={context.api} />
          <ul>
            <ItemLink id="apps" capabilities={capabilities} />
            <ItemLink id="organization" capabilities={capabilities} />
            <ItemLink id="channels" capabilities={capabilities} />
            <ItemLink id="incidents" capabilities={capabilities} />
          </ul>
          {allows(capabilities, ORGANIZATION_INCIDENTS) && <OpenIncidents context={context} />}
          {capabilities.organization["users.list"] === true && (
            <p className="muted">
              Its users are managed with <code>scripts/manage.py</code> until the users screen is
              built.
            </p>
          )}
          {capabilities.platform["monitoring.read"] !== true && (
            <p className="muted">
              Monitoring for one organization is not built yet: the monitoring shows every
              organization at once, and only the platform&apos;s administrators open it.
            </p>
          )}
        </section>
      )}
      {applicationsOnly && (
        <section aria-labelledby="home-applications">
          <h3 id="home-applications">Your applications</h3>
          <ul>
            {capabilities.applications.map((application) => {
              const id = application.application_id;
              // Each tab of its page the gateway allows on it (docs/56).
              return (
                <li key={id}>
                  <strong>{id}</strong>
                  {appTabs(capabilities, id).map((tab) => (
                    <a
                      key={tab}
                      className="home-link"
                      href={routeHash({ screen: "app", applicationId: id, tab, incidentId: null })}
                    >
                      {APP_TAB_WORDS[tab]}
                    </a>
                  ))}
                </li>
              );
            })}
          </ul>
        </section>
      )}
      {nothing && (
        <section aria-labelledby="home-nothing">
          <h3 id="home-nothing">No access yet</h3>
          <p>
            This account has no access to any part of the gateway yet. Ask your organization&apos;s
            administrator.
          </p>
          <div className="form-actions">
            <button type="button" onClick={onLogout}>
              Log out
            </button>
          </div>
        </section>
      )}
    </section>
  );
}

/** Where the login would be, in simulated live mode: it has no accounts (docs/50). */
export function NoAccounts() {
  return (
    <main className="console">
      <section className="panel login" aria-labelledby="no-accounts-title">
        <h2 id="no-accounts-title">This mode has no accounts</h2>
        <p>
          The gateway is in simulated live mode, which runs without logins, so the console&apos;s
          pages (settings, alert rules, channels and incidents) are not here. Everything this mode
          shows is simulated.
        </p>
        <p>
          <a href="#/monitor/overview">Open the monitoring</a>
        </p>
        <p className="muted">
          The console needs the plain server, with accounts: see docs/47 and docs/50.
        </p>
      </section>
    </main>
  );
}
