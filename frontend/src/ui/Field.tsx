import {
  useId,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from "react";

interface FieldText {
  label: ReactNode;
  /** Said under the field and tied to it (`aria-describedby`). */
  hint?: ReactNode;
  /** The gateway's answer about this field, shown after it answers; the field adds no rule. */
  error?: string | null;
}

function useField(hint: ReactNode, error: string | null | undefined, extra?: string) {
  const id = useId();
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  const invalid = error !== undefined && error !== null && error !== "";
  const describedBy =
    [extra, hint !== undefined ? hintId : null, invalid ? errorId : null]
      .filter((x) => x !== null && x !== undefined)
      .join(" ") || undefined;
  return { id, hintId, errorId, invalid, describedBy };
}

function Under({
  hint,
  hintId,
  error,
  errorId,
  invalid,
}: {
  hint: ReactNode;
  hintId: string;
  error: string | null | undefined;
  errorId: string;
  invalid: boolean;
}) {
  return (
    <>
      {hint !== undefined && (
        <p id={hintId} className="field-hint">
          {hint}
        </p>
      )}
      {invalid && (
        <p id={errorId} className="field-error">
          {error}
        </p>
      )}
    </>
  );
}

/** A labelled text input: the label always visible, the hint and the error tied to it. */
export function TextField({
  label,
  hint,
  error,
  className,
  ...input
}: FieldText & InputHTMLAttributes<HTMLInputElement>) {
  const f = useField(hint, error);
  return (
    <div className={className === undefined ? "field" : `field ${className}`}>
      <label htmlFor={f.id}>{label}</label>
      <input id={f.id} aria-invalid={f.invalid} aria-describedby={f.describedBy} {...input} />
      <Under {...f} hint={hint} error={error} />
    </div>
  );
}

/** A number with its unit written after it (and read with it: the unit describes the field). */
export function NumberField({
  label,
  hint,
  error,
  unit,
  className,
  ...input
}: FieldText & { unit?: string } & InputHTMLAttributes<HTMLInputElement>) {
  const unitId = useId();
  const f = useField(hint, error, unit === undefined ? undefined : unitId);
  return (
    <div className={className === undefined ? "field" : `field ${className}`}>
      <label htmlFor={f.id}>{label}</label>
      <div className="input-with-unit">
        <input
          id={f.id}
          type="number"
          inputMode="numeric"
          aria-invalid={f.invalid}
          aria-describedby={f.describedBy}
          {...input}
        />
        {unit !== undefined && (
          <span id={unitId} className="unit">
            {unit}
          </span>
        )}
      </div>
      <Under {...f} hint={hint} error={error} />
    </div>
  );
}

export function SelectField({
  label,
  hint,
  error,
  className,
  children,
  ...select
}: FieldText & SelectHTMLAttributes<HTMLSelectElement>) {
  const f = useField(hint, error);
  return (
    <div className={className === undefined ? "field" : `field ${className}`}>
      <label htmlFor={f.id}>{label}</label>
      <select id={f.id} aria-invalid={f.invalid} aria-describedby={f.describedBy} {...select}>
        {children}
      </select>
      <Under {...f} hint={hint} error={error} />
    </div>
  );
}

export function TextareaField({
  label,
  hint,
  error,
  className,
  ...area
}: FieldText & TextareaHTMLAttributes<HTMLTextAreaElement>) {
  const f = useField(hint, error);
  return (
    <div className={className === undefined ? "field" : `field ${className}`}>
      <label htmlFor={f.id}>{label}</label>
      <textarea id={f.id} aria-invalid={f.invalid} aria-describedby={f.describedBy} {...area} />
      <Under {...f} hint={hint} error={error} />
    </div>
  );
}
