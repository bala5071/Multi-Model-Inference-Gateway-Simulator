import { useState } from "react";
import { ApiError, applicationPath } from "./api";
import type { ConsoleContext } from "./context";
import { formatTime, refusalText } from "./derive";
import { KeyShownOnce } from "./KeyShownOnce";
import { useLoad } from "./useLoad";
import type { ApiKeySummary, IssuedKey, KeyPage } from "./types";
import { Button } from "../ui/Button";
import { DataTable } from "../ui/DataTable";
import { ConfirmDialog } from "../ui/Dialog";
import { NumberField, TextField } from "../ui/Field";
import { Skeleton } from "../ui/Skeleton";
import { StatusPill, toneOf } from "../ui/StatusPill";

function failure(error: unknown): string {
  if (error instanceof ApiError) return refusalText(error.code, error.message);
  return "Something went wrong reading the gateway's answer.";
}

function when(ms: number | null, none: string): string {
  return ms === null ? none : formatTime(ms);
}

/**
 * An application's API keys (docs/51): the list, and Issue, Rotate and Revoke. Those three are
 * shown once the gateway has answered the list: their rows equal the list's
 * (`application_keys.list`), pinned by tests/test_console_probes.py, since a probe of a step-up
 * write would be an audited refusal. Each still asks for the password (step-up), and the gateway
 * still checks it. A new key is shown once and forgotten.
 */
export function KeysScreen({
  context,
  applicationId,
}: {
  context: ConsoleContext;
  applicationId: string;
}) {
  return (
    <section className="console-screen" aria-label="API keys">
      <ApplicationKeys key={applicationId} context={context} applicationId={applicationId} />
    </section>
  );
}

function ApplicationKeys({
  context,
  applicationId,
}: {
  context: ConsoleContext;
  applicationId: string;
}) {
  const base = `${applicationPath(context.user.organization_id, applicationId)}/keys`;
  const [version, setVersion] = useState(0);
  const listed = useLoad(`keys:${base}:${version.toString()}`, () =>
    context.api.get<KeyPage>(`${base}?limit=100`),
  );
  const [shown, setShown] = useState<IssuedKey | null>(null);
  const [label, setLabel] = useState("");
  const [graceHours, setGraceHours] = useState("24");
  const [revoking, setRevoking] = useState<ApiKeySummary | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function act(run: () => Promise<IssuedKey | null>) {
    setBusy(true);
    setError(null);
    try {
      const issued = await context.guarded(run);
      if (issued !== null) setShown(issued);
      setVersion((v) => v + 1);
    } catch (caught) {
      setError(failure(caught));
    } finally {
      setBusy(false);
    }
  }

  if (listed.kind === "loading") return <Skeleton label="Loading keys…" />;
  if (listed.kind === "failed") {
    return (
      <p className="form-error" role="alert">
        {refusalText(listed.error.code, listed.error.message)}
      </p>
    );
  }
  const keys = listed.value.keys;
  return (
    <>
      {shown !== null && (
        <KeyShownOnce
          value={shown.key}
          label={shown.label}
          onDone={() => {
            setShown(null);
          }}
        />
      )}
      <DataTable
        label="Keys"
        rows={keys}
        rowKey={(k) => k.key_id}
        empty="This application has no keys yet."
        columns={[
          {
            key: "label",
            header: "Label",
            cell: (k) => k.label,
            rowHeader: true,
            sortValue: (k) => k.label,
          },
          { key: "id", header: "Key id", cell: (k) => <code className="figure">{k.key_id}</code> },
          {
            key: "state",
            header: "State",
            cell: (k) => <StatusPill tone={toneOf(k.state)}>{k.state}</StatusPill>,
            sortValue: (k) => k.state,
          },
          {
            key: "created",
            header: "Created",
            cell: (k) => formatTime(k.created_at_ms),
            sortValue: (k) => k.created_at_ms,
          },
          { key: "expires", header: "Expires", cell: (k) => when(k.expires_at_ms, "never") },
          {
            key: "used",
            header: "Last used",
            cell: (k) => when(k.last_used_at_ms, "not yet"),
            sortValue: (k) => k.last_used_at_ms,
          },
          {
            key: "change",
            header: "Change",
            cell: (k) =>
              k.state !== "revoked" && (
                <div className="row-actions">
                  <Button
                    size="sm"
                    disabled={busy}
                    onClick={() => {
                      const hours = Number(graceHours);
                      void act(() =>
                        context.api.send<IssuedKey>("POST", `${base}/${k.key_id}/rotate`, {
                          grace_hours: hours,
                        }),
                      );
                    }}
                  >
                    Rotate {k.label}
                  </Button>
                  <Button
                    size="sm"
                    variant="danger"
                    disabled={busy}
                    onClick={() => {
                      setRevoking(k);
                    }}
                  >
                    Revoke {k.label}
                  </Button>
                </div>
              ),
          },
        ]}
      />
      <ConfirmDialog
        open={revoking !== null}
        title="Revoke a key"
        confirmLabel="Revoke"
        busy={busy}
        onCancel={() => {
          setRevoking(null);
        }}
        onConfirm={() => {
          if (revoking === null) return;
          const id = revoking.key_id;
          // The confirmation closes first: a step-up, if asked, is its own dialog.
          setRevoking(null);
          void act(async () => {
            await context.api.send("POST", `${base}/${id}/revoke`, {});
            return null;
          });
        }}
      >
        {revoking !== null && (
          <p>
            Revoke <strong>{revoking.label}</strong> ({revoking.key_id})? Requests with it are
            refused at once. This cannot be undone.
          </p>
        )}
      </ConfirmDialog>
      <form
        className="panel"
        aria-label="Issue a key"
        onSubmit={(event) => {
          event.preventDefault();
          void act(() => context.api.send<IssuedKey>("POST", base, { label }));
        }}
      >
        <h3>Issue a key</h3>
        <TextField
          label="Label"
          value={label}
          maxLength={64}
          required
          onChange={(event) => {
            setLabel(event.target.value);
          }}
        />
        <NumberField
          label="When rotating, keep the old key for (hours)"
          unit="hours"
          min={0}
          max={168}
          value={graceHours}
          onChange={(event) => {
            setGraceHours(event.target.value);
          }}
        />
        <div className="form-actions">
          <Button type="submit" variant="primary" loading={busy} disabled={label === ""}>
            Issue a key
          </Button>
        </div>
        <p className="muted">Each change asks for your password again. A new key is shown once.</p>
      </form>
      {error !== null && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
    </>
  );
}
