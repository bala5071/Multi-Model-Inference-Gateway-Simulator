import { X } from "lucide-react";
import { useId, useState } from "react";

/**
 * A list of values as chips: each removable by its own button (named after its value), one added
 * by typing it and pressing Enter or Add. Nothing is judged here: the gateway checks the list.
 */
export function Chips({
  label,
  values,
  onChange,
  hint,
}: {
  label: string;
  values: readonly string[];
  onChange: (values: string[]) => void;
  hint?: string;
}) {
  const [text, setText] = useState("");
  const id = useId();
  const typed = text.trim();

  function add() {
    if (typed === "") return;
    onChange([...values, typed]);
    setText("");
  }

  return (
    <fieldset className="chips-field" aria-describedby={hint === undefined ? undefined : `${id}-hint`}>
      <legend>{label}</legend>
      <ul className="chips" aria-label={`${label}: values`}>
        {values.length === 0 && <li className="muted">none</li>}
        {values.map((value, index) => (
          // Values may repeat while being typed; the position tells them apart.
          <li key={`${index}:${value}`} className="chip">
            <span>{value}</span>
            <button
              type="button"
              className="chip-remove"
              aria-label={`Remove ${value} from ${label}`}
              onClick={() => {
                onChange(values.filter((_, i) => i !== index));
              }}
            >
              <X aria-hidden="true" focusable="false" size={14} />
            </button>
          </li>
        ))}
      </ul>
      <div className="chips-add">
        <input
          id={id}
          aria-label={`Add to ${label}`}
          value={text}
          spellCheck={false}
          onChange={(event) => {
            setText(event.target.value);
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              add();
            }
          }}
        />
        <button type="button" className="button secondary small" disabled={typed === ""} onClick={add}>
          Add
        </button>
      </div>
      {hint !== undefined && (
        <p id={`${id}-hint`} className="field-hint">
          {hint}
        </p>
      )}
    </fieldset>
  );
}
