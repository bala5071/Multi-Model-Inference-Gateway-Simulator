import type { ReactNode } from "react";

export type Tone = "ok" | "warn" | "bad" | "idle" | "info";

/** A state as a colour and its word, never the colour alone (docs/52 section 4). */
export function StatusPill({ tone, children }: { tone: Tone; children: ReactNode }) {
  return (
    <span className={`pill pill-${tone}`}>
      <span className="pill-dot" aria-hidden="true" />
      {children}
    </span>
  );
}

/**
 * The tone of a state the gateway names, for its pill. Only the colour is chosen here; the word
 * shown is always the gateway's own. A word not listed is drawn neutral.
 */
export function toneOf(state: string): Tone {
  const tones: Record<string, Tone> = {
    firing: "bad",
    failed: "bad",
    failing: "bad",
    dead: "bad",
    pending: "warn",
    acknowledged: "warn",
    retrying: "warn",
    unverified: "warn",
    resolved: "ok",
    active: "ok",
    delivered: "ok",
    sent: "ok",
    verified: "ok",
    enabled: "ok",
    yes: "ok",
    ok: "ok",
    revoked: "idle",
    expired: "idle",
    disabled: "idle",
    no: "idle",
  };
  return tones[state] ?? "info";
}
