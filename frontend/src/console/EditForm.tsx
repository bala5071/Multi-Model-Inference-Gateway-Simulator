import { useEffect, useState } from "react";
import { ApiError } from "./api";
import type { ConsoleContext } from "./context";
import {
  canSave,
  draftChange,
  initialDraft,
  refusalText,
  verdict,
  type Draft,
  type FieldMode,
} from "./derive";
import type { ApplicationSettings, Change, Validation } from "./types";
import { TableScroll } from "../components/TableScroll";

/**
 * The edit form of an application's settings. Every change is sent to the gateway's dry run
 * (`validate`) and its answer shown before "Save" is offered; when the dry run says the change
 * loosens where prompts go, "Save" asks for the password first.
 */
export function EditForm({
  context,
  base,
  settings,
  onSaved,
  onConflict,
}: {
  context: ConsoleContext;
  base: string;
  settings: ApplicationSettings;
  onSaved: (change: Change) => void;
  onConflict: () => void;
}) {
  const [draft, setDraft] = useState<Draft>(() => initialDraft(settings));
  const [validation, setValidation] = useState<{ key: string; answer: Validation } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const change = draftChange(draft);
  const key = JSON.stringify([settings.version, change.set, change.unset]);
  const ready = !change.empty && change.unparsed.length === 0;

  useEffect(() => {
    if (!ready) return;
    let current = true;
    context.api
      .send<Validation>("POST", `${base}/validate`, {
        expected_version: settings.version,
        set: change.set,
        unset: change.unset,
      })
      .then(
        (answer) => {
          if (current) setValidation({ key, answer });
        },
        (failure: unknown) => {
          if (current && failure instanceof ApiError) {
            setError(refusalText(failure.code, failure.message));
          }
        },
      );
    return () => {
      current = false;
    };
    // `key` is the change and the version it is made from.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, ready]);

  const shown = verdict(change, validation?.key === key ? validation.answer : null);

  function update(name: string, mode: FieldMode, text?: string) {
    setError(null);
    setDraft((old) => {
      const field = old[name] ?? { mode: "keep", text: "null" };
      return { ...old, [name]: { mode, text: text ?? field.text } };
    });
  }

  async function save() {
    setBusy(true);
    setError(null);
    try {
      const saved = await context.guarded(
        () =>
          context.api.send<Change>("PUT", base, {
            expected_version: settings.version,
            set: change.set,
            unset: change.unset,
          }),
        shown.kind === "valid" && shown.stepUp,
      );
      onSaved(saved);
    } catch (failure) {
      if (failure instanceof ApiError) {
        setError(refusalText(failure.code, failure.message));
        if (failure.code === "settings_version_conflict") onConflict();
      }
      setBusy(false);
    }
  }

  return (
    <section className="panel" aria-labelledby="edit-title">
      <h3 id="edit-title">Edit</h3>
      <p className="muted">
        Values are JSON. Each change is checked by the gateway before it can be saved.
      </p>
      <TableScroll table="Edit">
        <table className="data-table edit-table" aria-label="Edit">
          <thead>
            <tr>
              <th scope="col">Setting</th>
              <th scope="col">Change</th>
              <th scope="col">Value (JSON)</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(draft).map(([name, field]) => (
              <tr key={name}>
                <th scope="row">{name}</th>
                <td>
                  <select
                    aria-label={`${name}: change`}
                    value={field.mode}
                    onChange={(event) => {
                      update(name, event.target.value as FieldMode);
                    }}
                  >
                    <option value="keep">Keep</option>
                    <option value="set">Set</option>
                    <option value="unset">Unset</option>
                  </select>
                </td>
                <td>
                  <input
                    aria-label={`${name}: value`}
                    value={field.text}
                    disabled={field.mode !== "set"}
                    onChange={(event) => {
                      update(name, "set", event.target.value);
                    }}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </TableScroll>
      <div className="verdict" role="status" aria-label="Check result">
        {shown.kind === "nothing" && <p className="muted">No change yet.</p>}
        {shown.kind === "unparsed" && (
          <p className="form-error">Not JSON: {shown.fields.join(", ")}.</p>
        )}
        {shown.kind === "checking" && <p className="muted">Checking…</p>}
        {shown.kind === "invalid" && (
          <>
            <p className="form-error">
              Refused{shown.setting !== null ? ` (${shown.setting})` : ""}: {shown.message}
            </p>
            {shown.refusals.length > 0 && <p className="form-error">{shown.refusals.join("; ")}</p>}
          </>
        )}
        {shown.kind === "valid" && (
          <>
            <p>
              {shown.changed.length === 0
                ? "Valid, but it changes nothing."
                : `Valid. Changes: ${shown.changed.join(", ")}.`}
            </p>
            {shown.effective.length > 0 && (
              <p className="muted">
                In force after saving:{" "}
                {shown.effective.map(([name, value]) => `${name} ${value}`).join("; ")}
              </p>
            )}
            {shown.stepUp && (
              <p className="notice">
                This loosens where prompts may go: saving asks for your password.
              </p>
            )}
          </>
        )}
      </div>
      {error !== null && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
      <div className="form-actions">
        <button
          type="button"
          disabled={busy || !canSave(shown)}
          onClick={() => {
            void save();
          }}
        >
          Save
        </button>
      </div>
    </section>
  );
}
