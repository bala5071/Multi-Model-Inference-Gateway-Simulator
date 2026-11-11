import { useState } from "react";
import { ApiError, type Api } from "./api";
import { refusalText } from "./derive";
import { Button } from "../ui/Button";
import { Dialog } from "../ui/Dialog";

/**
 * Asks for the password once more before a change the gateway says needs it (docs/29). The
 * password lives in this form's field until it is sent, then the field is emptied; it is
 * never stored, and the dialog is gone once the gateway has accepted it. On the native `<dialog>`
 * (docs/55): the focus starts in it, the page behind is inert, Escape cancels.
 */
export function StepUpDialog({
  api,
  onDone,
  onCancel,
}: {
  api: Api;
  onDone: () => void;
  onCancel: () => void;
}) {
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
      await api.send("POST", "/v1/auth/step-up", { password: given });
      onDone();
    } catch (failure) {
      setError(
        failure instanceof ApiError
          ? refusalText(failure.code, failure.message)
          : "The password could not be checked.",
      );
      setBusy(false);
    }
  }

  return (
    <Dialog open title="Confirm with your password" onClose={onCancel}>
      <p className="muted">
        This change needs your password again. It is sent once to the gateway and not kept.
      </p>
      <form
        className="console-form"
        onSubmit={(event) => {
          void submit(event);
        }}
      >
        <label>
          Password
          <input
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => {
              setPassword(event.target.value);
            }}
            disabled={busy}
            required
          />
        </label>
        {error !== null && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <div className="form-actions dialog-actions">
          <Button variant="secondary" onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
          <Button
            type="submit"
            variant="primary"
            loading={busy}
            loadingText="Checking…"
            disabled={password === ""}
          >
            Confirm
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
