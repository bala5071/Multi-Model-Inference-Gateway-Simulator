import { useState } from "react";

/**
 * A new API key, shown this once: the gateway keeps only its HMAC and can never show it again
 * (docs/51). Held in the caller's state only, until "I have stored it" (or leaving the screen)
 * forgets it; never in storage, the address or the console log.
 */
export function KeyShownOnce({
  value,
  label,
  onDone,
}: {
  value: string;
  label: string;
  onDone: () => void;
}) {
  const [copied, setCopied] = useState<string | null>(null);
  return (
    <section className="panel signing-key" aria-label="New API key">
      <h3>New API key: {label}</h3>
      <p className="notice">Shown once. It cannot be shown again.</p>
      <p className="muted">
        Store it now where the application reads it. If it is lost, issue a new one and revoke this
        one.
      </p>
      <label className="key-field">
        API key
        <input className="key" readOnly value={value} aria-label="API key" />
      </label>
      <div className="form-actions">
        <button
          type="button"
          onClick={() => {
            navigator.clipboard.writeText(value).then(
              () => {
                setCopied("Copied.");
              },
              () => {
                setCopied("Not copied: select the key and copy it.");
              },
            );
          }}
        >
          Copy the key
        </button>
        <button type="button" onClick={onDone}>
          I have stored it
        </button>
      </div>
      {copied !== null && (
        <p className="muted" role="status">
          {copied}
        </p>
      )}
    </section>
  );
}
