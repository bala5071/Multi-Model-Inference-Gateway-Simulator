import { useCallback, useEffect, useState } from "react";
import { useTheme } from "../hooks/useTheme";
import { ApiError, sessionApi } from "./api";
import { ApplicationScreen } from "./ApplicationScreen";
import type { ConsoleContext } from "./context";
import { parseRoute, routeHash, type Route } from "./derive";
import { ChangePasswordScreen, LoginScreen } from "./LoginScreen";
import { OrganizationScreen } from "./OrganizationScreen";
import { StepUpDialog } from "./StepUpDialog";
import type { LoginAnswer, MeAnswer, SessionUser } from "./types";

type Session =
  { kind: "checking" } | { kind: "out"; notice: string | null } | { kind: "in"; user: SessionUser };

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
 * The admin console (C1): an organization's and its applications' settings. It logs in, sends
 * the session's CSRF token on every change and asks for the password when the gateway wants a
 * step-up; every rule (who may, what is valid, what is capped) is the gateway's. The CSRF token
 * is held in memory only: a reload asks `/v1/auth/me` for it again.
 */
export function ConsoleApp({ fetcher }: { fetcher?: typeof fetch }) {
  const [{ api, setCsrf }] = useState(() => sessionApi(fetcher));
  const [session, setSession] = useState<Session>({ kind: "checking" });
  const [asking, setAsking] = useState<Asking | null>(null);
  const [route, setRoute] = useRoute();
  const { theme, toggle } = useTheme();

  const enter = useCallback(
    (answer: LoginAnswer) => {
      setCsrf(answer.csrf_token);
      setSession({ kind: "in", user: answer.user });
    },
    [setCsrf],
  );

  const leave = useCallback(
    (notice: string | null) => {
      setCsrf(null);
      setAsking(null);
      setSession({ kind: "out", notice });
    },
    [setCsrf],
  );

  const check = useCallback(() => {
    api.get<MeAnswer>("/v1/auth/me").then(enter, (error: unknown) => {
      leave(
        error instanceof ApiError && error.status === 401
          ? null
          : "The gateway could not be asked who is logged in.",
      );
    });
  }, [api, enter, leave]);

  useEffect(check, [check]);

  const stepUp = useCallback(
    () =>
      new Promise<void>((resolve, reject) => {
        setAsking({ resolve, reject });
      }),
    [],
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
    leave("Logged out.");
  }

  const context: ConsoleContext | null =
    session.kind === "in" ? { api, user: session.user, guarded } : null;

  return (
    <div className="app console">
      <header className="top">
        <h1>Gateway console</h1>
        {session.kind === "in" && (
          <span className="muted">
            {session.user.username} ({session.user.organization_id})
          </span>
        )}
        <button type="button" onClick={toggle}>
          {theme === "dark" ? "Light theme" : "Dark theme"}
        </button>
        {session.kind === "in" && (
          <button
            type="button"
            onClick={() => {
              void logout();
            }}
          >
            Log out
          </button>
        )}
      </header>
      {session.kind === "checking" && <p className="muted">Checking the session…</p>}
      {session.kind === "out" && (
        <LoginScreen api={api} notice={session.notice} onLoggedIn={enter} />
      )}
      {session.kind === "in" && session.user.must_change_password && (
        <ChangePasswordScreen api={api} onChanged={check} />
      )}
      {context !== null && !context.user.must_change_password && (
        <>
          <nav className="console-nav" aria-label="Console sections">
            <a
              href={routeHash({
                screen: "application",
                applicationId: route.screen === "application" ? route.applicationId : null,
              })}
              aria-current={route.screen === "application" ? "page" : undefined}
            >
              Application settings
            </a>
            <a
              href={routeHash({ screen: "organization" })}
              aria-current={route.screen === "organization" ? "page" : undefined}
            >
              Organization settings
            </a>
          </nav>
          <main>
            {route.screen === "application" ? (
              <ApplicationScreen
                context={context}
                applicationId={route.applicationId}
                onChoose={(id) => {
                  setRoute({ screen: "application", applicationId: id });
                }}
              />
            ) : (
              <OrganizationScreen context={context} />
            )}
          </main>
        </>
      )}
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
