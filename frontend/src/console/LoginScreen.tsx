import { useState } from "react";
import { ApiError, type Api } from "./api";
import { refusalText } from "./derive";
import type { LoginAnswer } from "./types";

/** The console's login. The password is in the field until it is sent, then emptied. */
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
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

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
      setError(
        failure instanceof ApiError
          ? refusalText(failure.code, failure.message)
          : "The gateway's answer could not be read.",
      );
      setBusy(false);
    }
  }

  return (
    <section className="panel login" aria-labelledby="login-title">
      <h2 id="login-title">Log in</h2>
      {notice !== null && <p className="muted">{notice}</p>}
      <form
        className="console-form"
        onSubmit={(event) => {
          void submit(event);
        }}
      >
        <label>
          Username
          <input
            autoComplete="username"
            value={username}
            onChange={(event) => {
              setUsername(event.target.value);
            }}
            required
          />
        </label>
        <label>
          Password
          <input
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => {
              setPassword(event.target.value);
            }}
            required
          />
        </label>
        {error !== null && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <div className="form-actions">
          <button type="submit" disabled={busy}>
            Log in
          </button>
        </div>
      </form>
    </section>
  );
}

/** Shown instead of the console while the account must change its password (docs/27). */
export function ChangePasswordScreen({ api, onChanged }: { api: Api; onChanged: () => void }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

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
      setError(
        failure instanceof ApiError
          ? refusalText(failure.code, failure.message)
          : "The gateway's answer could not be read.",
      );
      setBusy(false);
    }
  }

  return (
    <section className="panel login" aria-labelledby="change-title">
      <h2 id="change-title">Change your password</h2>
      <p className="muted">This account must choose a new password before it can go on.</p>
      <form
        className="console-form"
        onSubmit={(event) => {
          void submit(event);
        }}
      >
        <label>
          Current password
          <input
            type="password"
            autoComplete="current-password"
            value={current}
            onChange={(event) => {
              setCurrent(event.target.value);
            }}
            required
          />
        </label>
        <label>
          New password
          <input
            type="password"
            autoComplete="new-password"
            value={next}
            onChange={(event) => {
              setNext(event.target.value);
            }}
            required
          />
        </label>
        {error !== null && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <div className="form-actions">
          <button type="submit" disabled={busy}>
            Change password
          </button>
        </div>
      </form>
    </section>
  );
}
