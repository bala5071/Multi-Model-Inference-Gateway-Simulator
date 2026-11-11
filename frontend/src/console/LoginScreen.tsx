import { Eye, EyeOff, LoaderCircle, LogOut, TriangleAlert } from "lucide-react";
import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { BrandMark } from "../shell/Brand";
import { ApiError, type Api } from "./api";
import { refusalText } from "./derive";
import { LOCKOUT_SENTENCE, PASSWORD_RULES } from "./passwordRules";
import type { LoginAnswer } from "./types";

/** The code of the one answer the gateway gives every refused login (docs/27). */
const REFUSED_LOGIN = "invalid_credentials";

function failureText(failure: unknown): { text: string; code: string | null } {
  if (failure instanceof ApiError) {
    return { text: refusalText(failure.code, failure.message), code: failure.code };
  }
  return { text: "The gateway's answer could not be read.", code: null };
}

/** The centred card both screens sit on: the mark, the heading, a helper line (docs/54). */
function AuthCard({
  titleId,
  title,
  helper,
  children,
}: {
  titleId: string;
  title: string;
  helper: string;
  children: ReactNode;
}) {
  return (
    <section className="auth-card" aria-labelledby={titleId}>
      <div className="auth-mark">
        <BrandMark size={36} />
      </div>
      <h2 id={titleId}>{title}</h2>
      <p className="auth-helper">{helper}</p>
      {children}
    </section>
  );
}

/**
 * A password field with a show/hide button and a Caps Lock warning. The value is the caller's
 * state, emptied once it has been sent; showing it changes the field's type only. `describedBy`
 * names help shown before anyone types (the rules).
 */
function PasswordField({
  label,
  autoComplete,
  value,
  onChange,
  describedBy,
  inputRef,
}: {
  label: string;
  autoComplete: "current-password" | "new-password";
  value: string;
  onChange: (value: string) => void;
  describedBy?: string;
  inputRef?: React.Ref<HTMLInputElement>;
}) {
  const id = useId();
  const capsId = useId();
  const [shown, setShown] = useState(false);
  const [capsLock, setCapsLock] = useState(false);
  const seeCaps = (event: KeyboardEvent<HTMLInputElement>) => {
    setCapsLock(event.getModifierState("CapsLock"));
  };
  const described = [describedBy, capsLock ? capsId : undefined].filter(Boolean).join(" ");
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <div className="password-input">
        <input
          ref={inputRef}
          id={id}
          type={shown ? "text" : "password"}
          autoComplete={autoComplete}
          autoCapitalize="none"
          spellCheck={false}
          value={value}
          aria-describedby={described === "" ? undefined : described}
          onChange={(event) => {
            onChange(event.target.value);
          }}
          onKeyDown={seeCaps}
          onKeyUp={seeCaps}
          onBlur={() => {
            setCapsLock(false);
          }}
          required
        />
        <button
          type="button"
          className="quiet password-toggle"
          aria-controls={id}
          aria-pressed={shown}
          aria-label={shown ? `Hide ${label.toLowerCase()}` : `Show ${label.toLowerCase()}`}
          onClick={() => {
            setShown(!shown);
          }}
        >
          {shown ? (
            <EyeOff aria-hidden="true" focusable="false" size={18} />
          ) : (
            <Eye aria-hidden="true" focusable="false" size={18} />
          )}
        </button>
      </div>
      {capsLock && (
        <p id={capsId} className="field-warning" role="status">
          <TriangleAlert aria-hidden="true" focusable="false" size={16} />
          Caps Lock is on.
        </p>
      )}
    </div>
  );
}

function SubmitButton({ busy, idle, working }: { busy: boolean; idle: string; working: string }) {
  return (
    <button type="submit" className="primary wide" disabled={busy} aria-busy={busy}>
      {busy && <LoaderCircle aria-hidden="true" focusable="false" size={18} className="spin" />}
      {busy ? working : idle}
    </button>
  );
}

/** The console's login (docs/54). The password is in its field until it is sent, then emptied. */
export function LoginScreen({
  api,
  notice,
  onLoggedIn,
}: {
  api: Api;
  notice: string | null;
  onLoggedIn: (answer: LoginAnswer) => void;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<{ text: string; code: string | null } | null>(null);
  const [busy, setBusy] = useState(false);
  const usernameRef = useRef<HTMLInputElement>(null);
  const usernameId = useId();
  const errorId = useId();

  useEffect(() => {
    usernameRef.current?.focus();
  }, []);

  async function submit(event: { preventDefault: () => void }) {
    event.preventDefault();
    const given = password;
    setPassword("");
    setBusy(true);
    setError(null);
    try {
      onLoggedIn(
        await api.send<LoginAnswer>("POST", "/v1/auth/login", { username, password: given }),
      );
    } catch (failure) {
      setError(failureText(failure));
      setBusy(false);
    }
  }

  return (
    <div className="auth-page">
      <AuthCard
        titleId="login-title"
        title="Log in"
        helper="Use the account your administrator gave you."
      >
        {notice !== null && (
          <p className="auth-notice" role="status">
            {notice}
          </p>
        )}
        <form
          className="auth-form"
          aria-describedby={error !== null ? errorId : undefined}
          onSubmit={(event) => {
            void submit(event);
          }}
        >
          <div className="field">
            <label htmlFor={usernameId}>Username</label>
            <input
              ref={usernameRef}
              id={usernameId}
              autoComplete="username"
              autoCapitalize="none"
              spellCheck={false}
              value={username}
              onChange={(event) => {
                setUsername(event.target.value);
              }}
              required
            />
          </div>
          <PasswordField
            label="Password"
            autoComplete="current-password"
            value={password}
            onChange={setPassword}
          />
          {error !== null && (
            <div id={errorId} className="form-error" role="alert">
              <p>{error.text}</p>
              {error.code === REFUSED_LOGIN && (
                <p className="form-error-more">{LOCKOUT_SENTENCE}</p>
              )}
            </div>
          )}
          <SubmitButton busy={busy} idle="Log in" working={"Logging in…"} />
        </form>
      </AuthCard>
    </div>
  );
}

/**
 * Shown instead of everything else while the account must change its password (docs/27, docs/54):
 * the form and Log out, nothing more. The rules are listed before anyone types.
 */
export function ChangePasswordScreen({
  api,
  onChanged,
  onLogout,
}: {
  api: Api;
  onChanged: () => void;
  onLogout?: () => void;
}) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const currentRef = useRef<HTMLInputElement>(null);
  const rulesId = useId();

  useEffect(() => {
    currentRef.current?.focus();
  }, []);

  async function submit(event: { preventDefault: () => void }) {
    event.preventDefault();
    const body = { current_password: current, new_password: next };
    setCurrent("");
    setNext("");
    setBusy(true);
    setError(null);
    try {
      await api.send("POST", "/v1/auth/change-password", body);
      onChanged();
    } catch (failure) {
      setError(failureText(failure).text);
      setBusy(false);
    }
  }

  return (
    <div className="auth-page">
      <AuthCard
        titleId="change-title"
        title="Change your password"
        helper="This account must choose a new password before it can go on."
      >
        <form
          className="auth-form"
          onSubmit={(event) => {
            void submit(event);
          }}
        >
          <PasswordField
            label="Current password"
            autoComplete="current-password"
            value={current}
            onChange={setCurrent}
            inputRef={currentRef}
          />
          <PasswordField
            label="New password"
            autoComplete="new-password"
            value={next}
            onChange={setNext}
            describedBy={rulesId}
          />
          <div id={rulesId} className="password-rules">
            <p>The new password must be:</p>
            <ul>
              {PASSWORD_RULES.map((rule) => (
                <li key={rule}>{rule}</li>
              ))}
            </ul>
          </div>
          {error !== null && (
            <p className="form-error" role="alert">
              {error}
            </p>
          )}
          <SubmitButton busy={busy} idle="Change password" working={"Changing the password…"} />
        </form>
        {onLogout !== undefined && (
          <div className="auth-secondary">
            <button type="button" className="quiet" onClick={onLogout}>
              <LogOut aria-hidden="true" focusable="false" size={16} />
              Log out
            </button>
          </div>
        )}
      </AuthCard>
    </div>
  );
}
