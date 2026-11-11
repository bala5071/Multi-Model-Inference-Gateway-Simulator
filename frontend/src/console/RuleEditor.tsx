import { useEffect, useState } from "react";
import { draftRule, initialRuleDraft, previewSummary, type RuleDraft } from "./alerts";
import { ApiError } from "./api";
import type { ConsoleContext } from "./context";
import { refusalText } from "./derive";
import type { AlertRule, RuleBody, RuleValidation } from "./types";
import { TableScroll } from "../components/TableScroll";

/** What the editor edits: an existing rule (saved with PUT), or a new one (POST). */
export interface Editing {
  rule: AlertRule | null;
  body: RuleBody;
}

/**
 * A rule's editor. Every field is JSON; every change is sent to the gateway's dry run
 * (`alert-rules/validate`, which writes nothing) and its answer shown, with what the rule
 * would have been over the last hour, before "Save" is offered.
 */
export function RuleEditor({
  context,
  base,
  editing,
  onSaved,
  onConflict,
  onCancel,
}: {
  context: ConsoleContext;
  base: string;
  editing: Editing;
  onSaved: (what: string) => void;
  onConflict: () => void;
  onCancel: () => void;
}) {
  const [draft, setDraft] = useState<RuleDraft>(() => initialRuleDraft(editing.body));
  const [answer, setAnswer] = useState<{ key: string; validation: RuleValidation } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const { body, unparsed } = draftRule(draft);
  const key = JSON.stringify(body);
  const ready = unparsed.length === 0;

  useEffect(() => {
    if (!ready) return;
    let current = true;
    context.api.send<RuleValidation>("POST", `${base}/validate`, body).then(
      (validation) => {
        if (current) setAnswer({ key, validation });
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
    // `key` is the body.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, ready]);

  const validation = answer?.key === key ? answer.validation : null;
  const summary =
    validation?.preview !== null && validation?.preview !== undefined
      ? previewSummary(validation.preview.minutes)
      : null;

  async function save() {
    setBusy(true);
    setError(null);
    try {
      if (editing.rule === null) {
        const made = await context.guarded(() => context.api.send<AlertRule>("POST", base, body));
        onSaved(`Created ${made.name}.`);
      } else {
        const rule = editing.rule;
        const saved = await context.guarded(() =>
          context.api.send<AlertRule>("PUT", `${base}/${encodeURIComponent(rule.id)}`, {
            expected_version: rule.version,
            rule: body,
          }),
        );
        onSaved(`Saved ${saved.name} as version ${saved.version}.`);
      }
    } catch (failure) {
      if (failure instanceof ApiError) {
        setError(refusalText(failure.code, failure.message));
        if (failure.code === "rule_version_conflict") onConflict();
      }
      setBusy(false);
    }
  }

  const title = editing.rule === null ? "New rule" : `Edit ${editing.rule.name}`;
  return (
    <section className="panel" aria-label="Rule editor">
      <h3>{title}</h3>
      <p className="muted">
        Values are JSON. Each change is checked by the gateway before it can be saved.
      </p>
      <TableScroll table="Rule fields">
        <table className="data-table edit-table" aria-label="Rule fields">
          <thead>
            <tr>
              <th scope="col">Field</th>
              <th scope="col">Value (JSON)</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(draft).map(([field, text]) => (
              <tr key={field}>
                <th scope="row">{field}</th>
                <td>
                  <input
                    aria-label={`${field}: value`}
                    value={text}
                    onChange={(event) => {
                      setError(null);
                      const next = event.target.value;
                      setDraft((old) => ({ ...old, [field]: next }));
                    }}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </TableScroll>
      <div className="verdict" role="status" aria-label="Rule check">
        {!ready && <p className="form-error">Not JSON: {unparsed.join(", ")}.</p>}
        {ready && validation === null && <p className="muted">Checking…</p>}
        {validation !== null && !validation.valid && (
          <p className="form-error">
            Refused{validation.invalid_field !== null ? ` (${validation.invalid_field})` : ""}:{" "}
            {validation.message}
          </p>
        )}
        {validation?.valid === true && (
          <>
            <p>Valid.</p>
            {summary === null ? (
              <p className="muted">
                No preview: the gateway does not compute this signal from the last hour&apos;s
                requests.
              </p>
            ) : (
              <>
                <p>
                  {summary.runs.length === 0
                    ? "Over the last hour the condition never held."
                    : `Over the last hour the condition held from ${summary.runs.join("; ")}.`}
                </p>
                <p className="muted">
                  {summary.tooFew} of {summary.total} minutes had too few requests to judge. Whether
                  it would have fired also depends on its for, cooldown and state, which the
                  evaluator decides.
                </p>
              </>
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
          disabled={busy || validation?.valid !== true}
          onClick={() => {
            void save();
          }}
        >
          Save rule
        </button>
        <button type="button" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
      </div>
    </section>
  );
}
