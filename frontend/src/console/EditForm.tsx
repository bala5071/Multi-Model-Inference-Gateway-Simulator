import { useEffect, useState, type ReactNode } from "react";
import { ApiError } from "./api";
import type { ConsoleContext } from "./context";
import { canSave, draftChange, refusalText, verdict, type Draft, type FieldMode } from "./derive";
import { kindOf, unitOf } from "./settingKinds";
import {
  ARROW,
  changedRows,
  fieldValue,
  withText,
  withUndo,
  withUnset,
  withValue,
} from "./settingsForm";
import type { Change, Json, Validation } from "./types";
import { TableScroll } from "../components/TableScroll";
import { Button } from "../ui/Button";
import { Chips } from "../ui/Chips";
import { NumberField, TextField } from "../ui/Field";
import { Toggle } from "../ui/Toggle";

function strings(value: Json | undefined): string[] {
  return Array.isArray(value)
    ? value.map((v) => (typeof v === "string" ? v : JSON.stringify(v)))
    : [];
}

/** The control for one setting, by its kind (`settingKinds.ts`); a JSON field where none is known. */
function Control({
  name,
  text,
  value,
  onText,
  onValue,
}: {
  name: string;
  text: string;
  value: Json | undefined;
  onText: (text: string) => void;
  onValue: (value: Json) => void;
}) {
  const kind = kindOf(name);
  if (kind === "bool") {
    return <Toggle label={name} checked={value === true} onChange={onValue} />;
  }
  if (kind === "int") {
    return (
      <NumberField
        label={name}
        unit={unitOf(name)}
        value={text === "null" ? "" : text}
        placeholder="not set"
        onChange={(event) => {
          onText(event.target.value === "" ? "null" : event.target.value);
        }}
      />
    );
  }
  if (kind === "tiers") {
    return <Chips label={name} values={strings(value)} onChange={onValue} />;
  }
  if (kind === "providers") {
    const all = value === "all";
    return (
      <div className="field">
        <Toggle
          label={`${name}: all providers`}
          checked={all}
          onChange={(on) => {
            onValue(on ? "all" : []);
          }}
        />
        {!all && <Chips label={name} values={strings(value)} onChange={onValue} />}
      </div>
    );
  }
  if (kind === "aliases") {
    const entries =
      value !== null && typeof value === "object" && !Array.isArray(value)
        ? Object.entries(value).map(([alias, tier]): [string, string] => [
            alias,
            typeof tier === "string" ? tier : JSON.stringify(tier),
          ])
        : [];
    const write = (next: [string, string][]) => {
      onValue(Object.fromEntries(next));
    };
    return (
      <fieldset className="chips-field">
        <legend>{name}</legend>
        {entries.length === 0 && <p className="muted">none</p>}
        {entries.map(([alias, tier], index) => (
          <div className="alias-row" key={index}>
            <input
              aria-label={`${name}: alias ${String(index + 1)}`}
              value={alias}
              spellCheck={false}
              onChange={(event) => {
                write(entries.map((e, i) => (i === index ? [event.target.value, e[1]] : e)));
              }}
            />
            <span aria-hidden="true">{ARROW}</span>
            <input
              aria-label={`${name}: tier for alias ${String(index + 1)}`}
              value={tier}
              spellCheck={false}
              onChange={(event) => {
                write(entries.map((e, i) => (i === index ? [e[0], event.target.value] : e)));
              }}
            />
            <Button
              size="sm"
              variant="quiet"
              aria-label={`Remove: alias ${String(index + 1)} of ${name}`}
              onClick={() => {
                write(entries.filter((_, i) => i !== index));
              }}
            >
              Remove
            </Button>
          </div>
        ))}
        <Button
          size="sm"
          onClick={() => {
            write([...entries, ["", ""]]);
          }}
        >
          Add an alias
        </Button>
      </fieldset>
    );
  }
  if (kind === "quality") {
    // Words, not JSON; nothing typed is "not set" (null), as for a number.
    return (
      <TextField
        label={name}
        value={typeof value === "string" ? value : ""}
        placeholder="not set"
        spellCheck={false}
        onChange={(event) => {
          onText(event.target.value === "" ? "null" : JSON.stringify(event.target.value));
        }}
      />
    );
  }
  return (
    <TextField
      label={`${name} (JSON)`}
      value={text}
      spellCheck={false}
      onChange={(event) => {
        onText(event.target.value);
      }}
    />
  );
}

/** Today's JSON editing (C1), under "Advanced": keep, set to the JSON typed, or unset. */
function JsonTable({
  draft,
  onField,
}: {
  draft: Draft;
  onField: (name: string, mode: FieldMode, text?: string) => void;
}) {
  return (
    <TableScroll table="Edit as JSON">
      <table className="data-table edit-table" aria-label="Edit as JSON">
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
                    onField(name, event.target.value as FieldMode);
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
                    onField(name, "set", event.target.value);
                  }}
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </TableScroll>
  );
}

/**
 * The typed settings form (docs/55), an application's or an organization's: a control for each
 * setting by its kind, with its source and cap beside it (`facts`); the changes listed with their
 * old and new values; every change sent to the gateway's dry run (`validate`), whose answer is
 * shown before Save is offered; Save, in a bar that stays in view while anything is changed, asks
 * for the password first when the dry run says the change loosens where prompts go. The JSON
 * editing of before is under "Advanced", on the same draft.
 */
export function EditForm({
  context,
  base,
  version,
  initial,
  stored,
  facts,
  onSaved,
  onConflict,
}: {
  context: ConsoleContext;
  base: string;
  version: number;
  initial: Draft;
  /** The settings this level has set itself: only those can go back to the inherited value. */
  stored: ReadonlySet<string>;
  facts: (name: string) => ReactNode;
  onSaved: (change: Change) => void;
  onConflict: () => void;
}) {
  const [draft, setDraft] = useState<Draft>(initial);
  const [validation, setValidation] = useState<{ key: string; answer: Validation } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const change = draftChange(draft);
  const key = JSON.stringify([version, change.set, change.unset]);
  const ready = !change.empty && change.unparsed.length === 0;

  useEffect(() => {
    if (!ready) return;
    let current = true;
    context.api
      .send<Validation>("POST", `${base}/validate`, {
        expected_version: version,
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
  const changed = changedRows(draft, initial);

  function edit(next: (old: Draft) => Draft) {
    setError(null);
    setDraft(next);
  }

  function onField(name: string, mode: FieldMode, text?: string) {
    edit((old) => {
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
            expected_version: version,
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
      <p className="muted">Each change is checked by the gateway before it can be saved.</p>
      <ul className="setting-list">
        {Object.entries(draft).map(([name, field]) => (
          <li key={name} className={field.mode === "keep" ? "setting-row" : "setting-row changed"}>
            <div>
              <div className="setting-name">{name}</div>
              <div className="setting-facts">{facts(name)}</div>
            </div>
            <div>
              {field.mode === "unset" ? (
                <p className="muted">Back to the inherited value when saved.</p>
              ) : (
                <Control
                  name={name}
                  text={field.text}
                  value={fieldValue(field)}
                  onText={(text) => {
                    edit((old) => withText(old, initial, name, text));
                  }}
                  onValue={(value) => {
                    edit((old) => withValue(old, initial, name, value));
                  }}
                />
              )}
              <div className="row-actions">
                {stored.has(name) && field.mode !== "unset" && (
                  <Button
                    size="sm"
                    variant="quiet"
                    aria-label={`Reset to the inherited value: ${name}`}
                    onClick={() => {
                      edit((old) => withUnset(old, name));
                    }}
                  >
                    Reset to the inherited value
                  </Button>
                )}
                {field.mode !== "keep" && (
                  <Button
                    size="sm"
                    variant="quiet"
                    aria-label={`Undo: ${name}`}
                    onClick={() => {
                      edit((old) => withUndo(old, initial, name));
                    }}
                  >
                    Undo
                  </Button>
                )}
              </div>
            </div>
          </li>
        ))}
      </ul>
      <details className="advanced">
        <summary>Advanced: edit as JSON</summary>
        <JsonTable draft={draft} onField={onField} />
      </details>
      <section className="changes" aria-label="Changes">
        <h4>Changed</h4>
        {changed.length === 0 ? (
          <p className="muted">Nothing changed.</p>
        ) : (
          <ul className="changed-list">
            {changed.map((row) => (
              <li key={row.name}>
                <span className="setting-name">{row.name}</span>: <del>{row.before}</del> {ARROW}{" "}
                <ins>{row.after}</ins>
              </li>
            ))}
          </ul>
        )}
      </section>
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
      {!change.empty || change.unparsed.length > 0 ? (
        <div className="save-bar" role="region" aria-label="Save changes">
          <span className="save-bar-count">
            {changed.length === 1 ? "1 change" : `${String(changed.length)} changes`}
          </span>
          <Button
            variant="quiet"
            disabled={busy}
            onClick={() => {
              edit(() => initial);
            }}
          >
            Discard
          </Button>
          <Button
            variant="primary"
            loading={busy}
            disabled={!canSave(shown)}
            onClick={() => {
              void save();
            }}
          >
            Save
          </Button>
        </div>
      ) : null}
    </section>
  );
}
