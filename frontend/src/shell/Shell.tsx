import { useCallback, useEffect, useRef, useState } from "react";
import { App } from "../App";
import type { StreamOptions } from "../hooks/useMonitorStream";
import { ApiError, sessionApi } from "../console/api";
import { ApplicationScreen } from "../console/ApplicationScreen";
import { ChannelsScreen } from "../console/ChannelsScreen";
import type { ConsoleContext } from "../console/context";
import { IncidentsScreen } from "../console/IncidentsScreen";
import { ChangePasswordScreen, LoginScreen } from "../console/LoginScreen";
import { OrganizationScreen } from "../console/OrganizationScreen";
import { RulesScreen } from "../console/RulesScreen";
import { StepUpDialog } from "../console/StepUpDialog";
import type { Capabilities, LoginAnswer, MeAnswer, SessionUser } from "../console/types";
import { Header } from "./Header";
import { Home, NoAccounts } from "./Home";
import { allows, itemOf, LOAD_CONTROL, opens, PLAYGROUND } from "./navigation";
import { NavBar } from "./NavBar";
import { parseRoute, routeHash, type Route } from "./routes";

export const CAPABILITIES_PATH = "/v1/auth/capabilities";
export const SESSION_ENDED = "Your session has ended. Log in again.";
export const NOT_AVAILABLE = "That page is not available to this account.";

/**
 * Where the page stands with the gateway: asking; no accounts at all (simulated live mode, where
 * /v1/auth answers 404); logged out; logged in but must change the password; logged in, with
 * what the gateway says the session may do (null until it has answered, `failed` if it could
 * not).
 */
export type Session =
  | { kind: "checking" }
  | { kind: "none" }
  | { kind: "out"; notice: string | null }
  | { kind: "change"; user: SessionUser }
  | { kind: "in"; user: SessionUser; capabilities: Capabilities | null; failed: boolean };

interface Asking {
  resolve: () => void;
  reject: (error: ApiError) => void;
}

function useRoute(): [Route, (route: Route) => void] {
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.hash));
  useEffect(() => {
    const onChange = () => {
      setRoute(parseRoute(window.location.hash));
    };
    window.addEventListener("hashchange", onChange);
    return () => {
      window.removeEventListener("hashchange", onChange);
    };
  }, []);
  return [
    route,
    (next) => {
      window.location.hash = routeHash(next);
      setRoute(next);
    },
  ];
}

/**
 * The one page (docs/50): the monitoring and the console behind one login, one header and one
 * navigation. It holds no rule of its own: what a session may open is what
 * `GET /v1/auth/capabilities` says, asked again at each new section, and every screen still asks
 * the gateway before it shows a write control. The CSRF token is held in memory only.
 */
export function Shell({ fetcher, stream }: { fetcher?: typeof fetch; stream?: StreamOptions }) {
  const sessionRef = useRef<Session>({ kind: "checking" });
  const askingRef = useRef<Asking | null>(null);
  const [{ api, setCsrf, onSessionEnded }] = useState(() => sessionApi(fetcher));
  const [session, setSession] = useState<Session>({ kind: "checking" });
  const [asking, setAskingState] = useState<Asking | null>(null);
  const [simulated, setSimulated] = useState(false);
  const [route, setRoute] = useRoute();

  /** The session's next state: kept in the ref at once, for the callbacks that read it. */
  const move = useCallback((next: Session | ((now: Session) => Session)) => {
    sessionRef.current = typeof next === "function" ? next(sessionRef.current) : next;
    setSession(sessionRef.current);
  }, []);

  const setAsking = useCallback((next: Asking | null) => {
    askingRef.current = next;
    setAskingState(next);
  }, []);

  // A call answered 401 session_required while the page thought it was logged in: forget the
  // token, close any password prompt, and log in again where the person was (the hash stays).
  const ended = useCallback(() => {
    const now = sessionRef.current;
    if (now.kind !== "in" && now.kind !== "change") return;
    setCsrf(null);
    askingRef.current?.reject(new ApiError(401, "session_required", SESSION_ENDED));
    setAsking(null);
    move({ kind: "out", notice: SESSION_ENDED });
  }, [move, setAsking, setCsrf]);
  useEffect(() => {
    onSessionEnded(ended);
  }, [onSessionEnded, ended]);

  const enter = useCallback(
    (answer: LoginAnswer) => {
      setCsrf(answer.csrf_token);
      move(
        answer.user.must_change_password
          ? { kind: "change", user: answer.user }
          : { kind: "in", user: answer.user, capabilities: null, failed: false },
      );
    },
    [move, setCsrf],
  );

  const check = useCallback(() => {
    api.get<MeAnswer>("/v1/auth/me").then(enter, (error: unknown) => {
      if (error instanceof ApiError && error.status === 404) {
        move({ kind: "none" });
      } else if (error instanceof ApiError && error.status === 401) {
        if (sessionRef.current.kind !== "out") move({ kind: "out", notice: null });
      } else {
        move({ kind: "out", notice: "The gateway could not be asked who is logged in." });
      }
    });
  }, [api, enter, move]);

  useEffect(check, [check]);

  // What the session may do: asked when it logs in and again at each new section, never on a
  // timer. The last answer is kept while the next is on its way.
  const userId = session.kind === "in" ? session.user.user_id : null;
  const item = itemOf(route);
  useEffect(() => {
    if (userId === null) return;
    let current = true;
    api.get<Capabilities>(CAPABILITIES_PATH).then(
      (capabilities) => {
        if (current) {
          move((now) => (now.kind === "in" ? { ...now, capabilities, failed: false } : now));
        }
      },
      (error: unknown) => {
        if (!current || (error instanceof ApiError && error.status === 401)) return;
        if (error instanceof ApiError && error.code === "password_change_required") {
          move((now) => (now.kind === "in" ? { kind: "change", user: now.user } : now));
          return;
        }
        move((now) => (now.kind === "in" ? { ...now, failed: true } : now));
      },
    );
    return () => {
      current = false;
    };
  }, [api, move, userId, item]);

  const stepUp = useCallback(
    () =>
      new Promise<void>((resolve, reject) => {
        setAsking({ resolve, reject });
      }),
    [setAsking],
  );

  const guarded = useCallback(
    async <T,>(run: () => Promise<T>, askFirst = false): Promise<T> => {
      if (askFirst) await stepUp();
      try {
        return await run();
      } catch (error) {
        if (!(error instanceof ApiError) || error.code !== "step_up_required") throw error;
        await stepUp();
        return run();
      }
    },
    [stepUp],
  );

  async function logout() {
    try {
      await api.send("POST", "/v1/auth/logout");
    } catch {
      // The session is forgotten here whatever the gateway answered.
    }
    setCsrf(null);
    setAsking(null);
    // The next person starts at home, not on this person's screen.
    setRoute({ screen: "home" });
    move({ kind: "out", notice: "Logged out." });
  }

  // The monitoring's stream came back after a break: is the session still there?
  const recheck = useCallback(() => {
    if (sessionRef.current.kind === "in") void api.get("/v1/auth/me").catch(() => undefined);
  }, [api]);

  const user = session.kind === "in" || session.kind === "change" ? session.user : null;
  const capabilities = session.kind === "in" ? session.capabilities : null;

  function monitoring(tab: Extract<Route, { screen: "monitor" }>["tab"], everything: boolean) {
    return (
      <App
        tab={tab}
        onSelectTab={(next) => {
          setRoute({ screen: "monitor", tab: next });
        }}
        stream={stream}
        allows={
          everything || capabilities === null
            ? undefined
            : {
                playground: allows(capabilities, PLAYGROUND),
                loadControl: allows(capabilities, LOAD_CONTROL),
              }
        }
        onSimulated={setSimulated}
        onReconnect={everything ? undefined : recheck}
      />
    );
  }

  function body() {
    switch (session.kind) {
      case "checking":
        return (
          <main>
            <p className="muted">Checking the session…</p>
          </main>
        );
      case "none":
        // No accounts: the monitoring only. A console route gets the reason, not a login.
        if (route.screen === "home") return monitoring("overview", true);
        if (route.screen === "monitor") return monitoring(route.tab, true);
        return <NoAccounts />;
      case "out":
        return (
          <main className="console">
            <LoginScreen api={api} notice={session.notice} onLoggedIn={enter} />
          </main>
        );
      case "change":
        return (
          <main className="console">
            <ChangePasswordScreen api={api} onChanged={check} />
          </main>
        );
      case "in":
        return signedIn(session);
    }
  }

  function signedIn(now: Extract<Session, { kind: "in" }>) {
    if (now.capabilities === null) {
      return (
        <main>
          <p className="muted" role="status">
            {now.failed
              ? "The gateway could not say what this account may do. Reload the page to ask again."
              : "Asking the gateway what this account may do…"}
          </p>
        </main>
      );
    }
    const context: ConsoleContext = {
      api,
      user: now.user,
      capabilities: now.capabilities,
      guarded,
    };
    const home = (notice: string | null) => (
      <main className="console">
        <Home
          context={context}
          notice={notice}
          onLogout={() => {
            void logout();
          }}
        />
      </main>
    );
    // A place the session may not open, and one the page does not know, get the same words.
    if (!opens(route, now.capabilities))
      return home(route.screen === "home" ? null : NOT_AVAILABLE);
    switch (route.screen) {
      case "home":
      case "unknown":
        return home(null);
      case "monitor":
        return monitoring(route.tab, false);
      case "application":
        return (
          <main className="console">
            <ApplicationScreen
              context={context}
              applicationId={route.applicationId}
              onChoose={(id) => {
                setRoute({ screen: "application", applicationId: id });
              }}
            />
          </main>
        );
      case "organization":
        return (
          <main className="console">
            <OrganizationScreen context={context} />
          </main>
        );
      case "rules":
        return (
          <main className="console">
            <RulesScreen
              context={context}
              applicationId={route.applicationId}
              onChoose={(id) => {
                setRoute({ screen: "rules", applicationId: id });
              }}
            />
          </main>
        );
      case "channels":
        return (
          <main className="console">
            <ChannelsScreen context={context} />
          </main>
        );
      case "incidents":
        return (
          <main className="console">
            <IncidentsScreen
              context={context}
              applicationId={route.applicationId}
              incidentId={route.incidentId}
              onRoute={(applicationId, incidentId) => {
                setRoute({ screen: "incidents", applicationId, incidentId });
              }}
            />
          </main>
        );
    }
  }

  return (
    <div className="app">
      <Header
        user={user}
        capabilities={capabilities}
        simulated={simulated}
        onLogout={
          user === null
            ? null
            : () => {
                void logout();
              }
        }
      />
      {capabilities !== null && <NavBar capabilities={capabilities} route={route} />}
      {body()}
      {asking !== null && (
        <StepUpDialog
          api={api}
          onDone={() => {
            asking.resolve();
            setAsking(null);
          }}
          onCancel={() => {
            asking.reject(
              new ApiError(0, "step_up_cancelled", "Not changed: the password was not given."),
            );
            setAsking(null);
          }}
        />
      )}
    </div>
  );
}
