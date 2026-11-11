import { useState } from "react";
import { addressLines, channelRows, deliveryRows, followsProbe, recipientRows } from "./alerts";
import {
  ApiError,
  channelPath,
  orgPath,
  probeRefusedBody,
  PROBE_RECIPIENT,
  type WriteProbe,
} from "./api";
import type { ConsoleContext } from "./context";
import { refusalText } from "./derive";
import { EvaluatorBanner } from "./EvaluatorBanner";
import { useLoad } from "./useLoad";
import type {
  Channel,
  ChannelPage,
  CreatedChannel,
  DeliveryPage,
  Recipient,
  TestSend,
} from "./types";
import { Button } from "../ui/Button";
import { Card, StatTile, StatTiles } from "../ui/Card";
import { DataTable } from "../ui/DataTable";
import { ConfirmDialog } from "../ui/Dialog";
import { SelectField, TextareaField, TextField } from "../ui/Field";
import { Skeleton } from "../ui/Skeleton";
import { StatusPill, toneOf } from "../ui/StatusPill";
import { Toasted } from "../ui/Toasts";
import { Toggle } from "../ui/Toggle";

/**
 * A new signing key, shown this once: the gateway never shows it again. Held in this
 * component's state only, until "I have stored it" (or leaving the screen) forgets it; never
 * in storage, the URL or a log.
 */
function SigningKey({ value, onDone }: { value: string; onDone: () => void }) {
  const [copied, setCopied] = useState<string | null>(null);
  return (
    <section className="panel signing-key" aria-label="Signing key">
      <h3>Signing key: shown once</h3>
      <p className="notice">
        Store this key now, where the receiver checks signatures. The gateway will not show it
        again; if it is lost, rotate it.
      </p>
      <code className="key">{value}</code>
      <div className="form-actions">
        <Button
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
        </Button>
        <Button variant="primary" onClick={onDone}>
          I have stored it
        </Button>
      </div>
      {copied !== null && (
        <p className="muted" role="status">
          {copied}
        </p>
      )}
    </section>
  );
}

/**
 * Create a channel: a webhook (its URL, of which the gateway keeps the secret parts sealed and
 * shows only the origin) or an email channel (its recipients, each sent a confirmation code).
 * The URL is in this form's field until the gateway has taken it, then the field is emptied.
 */
function CreateChannel({
  context,
  onCreated,
}: {
  context: ConsoleContext;
  onCreated: (created: CreatedChannel) => void;
}) {
  const [type, setType] = useState("webhook");
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [recipients, setRecipients] = useState("");
  const [isDefault, setIsDefault] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: { preventDefault: () => void }) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    const body =
      type === "webhook"
        ? { type, name, url, default: isDefault }
        : { type, name, recipients: addressLines(recipients), default: isDefault };
    try {
      const created = await context.guarded(() =>
        context.api.send<CreatedChannel>(
          "POST",
          `${orgPath(context.user.organization_id)}/alert-channels`,
          body,
        ),
      );
      // No field is emptied here: the list is read again, which makes this form afresh, its
      // fields empty, the URL with them (tested).
      onCreated(created);
    } catch (failure) {
      if (failure instanceof ApiError) setError(refusalText(failure.code, failure.message));
    }
    setBusy(false);
  }

  return (
    <section className="panel" aria-label="New channel">
      <h3>New channel</h3>
      <form
        className="console-form"
        onSubmit={(event) => {
          void submit(event);
        }}
      >
        <SelectField
          label="Type"
          value={type}
          onChange={(event) => {
            setType(event.target.value);
          }}
        >
          <option value="webhook">webhook</option>
          <option value="email">email</option>
        </SelectField>
        <TextField
          label="Name"
          value={name}
          onChange={(event) => {
            setName(event.target.value);
          }}
        />
        {type === "webhook" ? (
          <TextField
            label="Webhook URL"
            hint="Only its origin is shown after this; the gateway keeps the rest sealed."
            value={url}
            autoComplete="off"
            spellCheck={false}
            onChange={(event) => {
              setUrl(event.target.value);
            }}
          />
        ) : (
          <TextareaField
            label="Recipients (one address a line)"
            value={recipients}
            rows={3}
            onChange={(event) => {
              setRecipients(event.target.value);
            }}
          />
        )}
        <Toggle
          label="An organization default (rules sending to the organization's defaults use it)"
          checked={isDefault}
          onChange={setIsDefault}
        />
        {error !== null && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <div className="form-actions">
          <Button type="submit" variant="primary" loading={busy}>
            Create channel
          </Button>
        </div>
      </form>
    </section>
  );
}

/** Confirm a recipient with the code mailed to it. The code is in the field until sent. */
function CodeEntry({
  onConfirm,
  busy,
  address,
}: {
  onConfirm: (code: string) => void;
  busy: boolean;
  address: string;
}) {
  const [code, setCode] = useState("");
  return (
    <form
      className="inline-form"
      onSubmit={(event) => {
        event.preventDefault();
        const given = code;
        setCode("");
        onConfirm(given);
      }}
    >
      <TextField
        label={`Code for ${address}`}
        value={code}
        autoComplete="one-time-code"
        spellCheck={false}
        onChange={(event) => {
          setCode(event.target.value);
        }}
      />
      <Button type="submit" size="sm" disabled={busy || code === ""}>
        Confirm
      </Button>
    </form>
  );
}

interface ChannelLoaded {
  channel: Channel;
  deliveries: DeliveryPage;
  confirmProbe: WriteProbe;
}

function ChannelView({
  context,
  channelId,
  canManage,
  onDeleted,
  onClose,
}: {
  context: ConsoleContext;
  channelId: string;
  canManage: boolean;
  onDeleted: (note: string) => void;
  onClose: () => void;
}) {
  const [reloads, setReloads] = useState(0);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [adding, setAdding] = useState("");
  const [deleting, setDeleting] = useState(false);
  const path = channelPath(context.user.organization_id, channelId);
  const loaded = useLoad(`channel:${path}:${reloads}`, async (): Promise<ChannelLoaded> => {
    const channel = await context.api.get<Channel>(path);
    const [deliveries, confirmProbe] = await Promise.all([
      context.api.get<DeliveryPage>(`${path}/deliveries?limit=20`),
      channel.type === "email"
        ? probeRefusedBody(context.api, `${path}/recipients/${PROBE_RECIPIENT}/confirm`)
        : Promise.resolve<WriteProbe>("forbidden"),
    ]);
    return { channel, deliveries, confirmProbe };
  });

  async function act<T>(run: () => Promise<T>, done: (answer: T) => string): Promise<void> {
    setBusy(true);
    setMessage(null);
    setError(null);
    try {
      const answer = await context.guarded(run);
      setMessage(done(answer));
    } catch (failure) {
      if (failure instanceof ApiError) setError(refusalText(failure.code, failure.message));
    }
    setBusy(false);
    setReloads((n) => n + 1);
  }

  const send = <T,>(method: "POST" | "DELETE", suffix: string, body?: unknown) =>
    context.api.send<T>(method, `${path}${suffix}`, body);

  return (
    <section className="panel" aria-label="Channel">
      <div className="form-actions">
        <Button variant="quiet" onClick={onClose}>
          Back to the list
        </Button>
      </div>
      <Toasted message={message} />
      {error !== null && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
      {loaded.kind === "loading" && <Skeleton />}
      {loaded.kind === "failed" && (
        <p className="form-error" role="alert">
          {refusalText(loaded.error.code, loaded.error.message)}
        </p>
      )}
      {loaded.kind === "ready" && (
        <>
          <h3>{loaded.value.channel.name}</h3>
          {channelRows([loaded.value.channel]).map((row) => (
            <StatTiles key={row.id}>
              <StatTile label="Type" value={row.type} />
              <StatTile
                label="Enabled"
                value={<StatusPill tone={toneOf(row.enabled)}>{row.enabled}</StatusPill>}
              />
              <StatTile label="Default" value={row.isDefault} />
              <StatTile label="Target" value={row.target} />
              <StatTile label="Last delivered" value={row.lastDelivered} />
              <StatTile label="Last failed" value={row.lastFailed} />
              <StatTile label="Failures in a row" value={row.failures} />
            </StatTiles>
          ))}
          {canManage && (
            <div className="form-actions">
              <Button
                disabled={busy}
                onClick={() => {
                  void act(
                    () => send<TestSend>("POST", "/test"),
                    (answer) => `Test send: ${answer.outcome}.`,
                  );
                }}
              >
                Test send
              </Button>
              {loaded.value.channel.enabled && (
                <Button
                  disabled={busy}
                  onClick={() => {
                    void act(
                      () => send("POST", "/disable"),
                      () => "Disabled.",
                    );
                  }}
                >
                  Disable
                </Button>
              )}
              <Button
                variant="danger"
                disabled={busy}
                onClick={() => {
                  setDeleting(true);
                }}
              >
                Delete
              </Button>
            </div>
          )}
          <ConfirmDialog
            open={deleting}
            title="Delete this channel"
            confirmLabel="Delete it"
            busy={busy}
            onCancel={() => {
              setDeleting(false);
            }}
            onConfirm={() => {
              // The confirmation closes first: a step-up, if asked, is its own dialog.
              setDeleting(false);
              setBusy(true);
              context
                .guarded(() => send("DELETE", ""))
                .then(
                  () => {
                    onDeleted(`Deleted ${loaded.value.channel.name}.`);
                  },
                  (failure: unknown) => {
                    if (failure instanceof ApiError) {
                      setError(refusalText(failure.code, failure.message));
                    }
                    setBusy(false);
                  },
                );
            }}
          >
            <p>
              Delete <strong>{loaded.value.channel.name}</strong>? Nothing more is sent through it.
              This cannot be undone.
            </p>
          </ConfirmDialog>
          {loaded.value.channel.type === "email" && (
            <>
              <h4>Recipients</h4>
              <p className="muted">
                Nothing is sent to an address until it is verified with the code mailed to it.
              </p>
              <DataTable
                label="Recipients"
                rows={recipientRows(loaded.value.channel.recipients ?? [])}
                rowKey={(r) => r.id}
                empty="No recipient."
                columns={[
                  { key: "address", header: "Address", cell: (r) => r.address, rowHeader: true },
                  {
                    key: "state",
                    header: "State",
                    cell: (r) => <StatusPill tone={toneOf(r.state)}>{r.state}</StatusPill>,
                  },
                  { key: "added", header: "Added", cell: (r) => r.added },
                  { key: "verified", header: "Verified", cell: (r) => r.verified },
                  {
                    key: "expires",
                    header: "Code valid until",
                    cell: (r) => (r.unverified ? r.expires : "-"),
                  },
                  {
                    key: "change",
                    header: "Change",
                    cell: (r) => (
                      <div className="row-actions">
                        {r.unverified && followsProbe(loaded.value.confirmProbe) && (
                          <>
                            <CodeEntry
                              address={r.address}
                              busy={busy}
                              onConfirm={(code) => {
                                void act(
                                  () =>
                                    send<Recipient>(
                                      "POST",
                                      `/recipients/${encodeURIComponent(r.id)}/confirm`,
                                      { code },
                                    ),
                                  (answer) => `${answer.address} is ${answer.state}.`,
                                );
                              }}
                            />
                            <Button
                              size="sm"
                              disabled={busy}
                              onClick={() => {
                                void act(
                                  () =>
                                    send<{ confirmation: string }>(
                                      "POST",
                                      `/recipients/${encodeURIComponent(r.id)}/resend`,
                                    ),
                                  (answer) => `Code sent again: ${answer.confirmation}.`,
                                );
                              }}
                            >
                              Send the code again to {r.address}
                            </Button>
                          </>
                        )}
                        {canManage && (
                          <Button
                            size="sm"
                            disabled={busy}
                            onClick={() => {
                              void act(
                                () => send("DELETE", `/recipients/${encodeURIComponent(r.id)}`),
                                () => `Removed ${r.address}.`,
                              );
                            }}
                          >
                            Remove {r.address}
                          </Button>
                        )}
                      </div>
                    ),
                  },
                ]}
              />
              {canManage && (
                <form
                  className="inline-form"
                  onSubmit={(event) => {
                    event.preventDefault();
                    const address = adding;
                    void act(
                      () =>
                        send<Recipient & { confirmation: string }>("POST", "/recipients", {
                          address,
                        }),
                      (answer) => {
                        setAdding("");
                        return `Added ${answer.address}: confirmation ${answer.confirmation}.`;
                      },
                    );
                  }}
                >
                  <TextField
                    label="Add a recipient"
                    value={adding}
                    onChange={(event) => {
                      setAdding(event.target.value);
                    }}
                  />
                  <Button type="submit" disabled={busy || adding === ""}>
                    Add
                  </Button>
                </form>
              )}
            </>
          )}
          <h4>Deliveries</h4>
          <DataTable
            label="Deliveries"
            rows={deliveryRows(loaded.value.deliveries.deliveries)}
            rowKey={(d) => d.id}
            empty="Nothing delivered yet."
            columns={[
              { key: "notification", header: "Notification", cell: (d) => d.notification },
              {
                key: "state",
                header: "State",
                cell: (d) => <StatusPill tone={toneOf(d.state)}>{d.state}</StatusPill>,
                sortValue: (d) => d.state,
              },
              { key: "attempts", header: "Attempts", cell: (d) => d.attempts },
              { key: "code", header: "Last code", cell: (d) => d.lastCode },
              { key: "next", header: "Next attempt", cell: (d) => d.next },
              {
                key: "updated",
                header: "Updated",
                cell: (d) => d.updated,
                sortValue: (d) => d.updated,
              },
            ]}
          />
        </>
      )}
    </section>
  );
}

/**
 * The organization's alert channels: the list with health; create (a webhook's signing key
 * shown once); an email channel's recipients, the code entry and re-send; test send and its
 * outcome; deliveries. A webhook's URL is never shown, only its origin: the gateway sends
 * nothing more. Who may change channels follows the list's own answer (note 49).
 */
export function ChannelsScreen({ context }: { context: ConsoleContext }) {
  const [reloads, setReloads] = useState(0);
  const [message, setMessage] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [signingKey, setSigningKey] = useState<string | null>(null);
  const path = `${orgPath(context.user.organization_id)}/alert-channels`;
  const loaded = useLoad(`channels:${path}:${reloads}`, () =>
    context.api.get<ChannelPage>(`${path}?limit=100`),
  );
  // The list answered: this account holds alert_channels.read, whose role row is the same as
  // write's, disable's and test's (pinned by tests/test_console_probes.py). Any refusal or
  // doubt shows no list and no control.
  const canManage = followsProbe(loaded.kind === "ready" ? "allowed" : null);

  return (
    <div className="console-screen">
      <h2>Channels</h2>
      <EvaluatorBanner api={context.api} />
      <Toasted message={message} />
      {signingKey !== null && (
        <SigningKey
          value={signingKey}
          onDone={() => {
            setSigningKey(null);
          }}
        />
      )}
      {loaded.kind === "loading" && <Skeleton />}
      {loaded.kind === "failed" && (
        <p className="form-error" role="alert">
          {refusalText(loaded.error.code, loaded.error.message)}
        </p>
      )}
      {loaded.kind === "ready" &&
        (selected !== null ? (
          <ChannelView
            key={selected}
            context={context}
            channelId={selected}
            canManage={canManage}
            onDeleted={(note) => {
              setMessage(note);
              setSelected(null);
              setReloads((n) => n + 1);
            }}
            onClose={() => {
              // The list is read again: what was changed in the channel shows there.
              setSelected(null);
              setReloads((n) => n + 1);
            }}
          />
        ) : (
          <>
            <Card title="Channel list" label="Channel list">
              <DataTable
                label="Channel list"
                rows={channelRows(loaded.value.channels)}
                rowKey={(row) => row.id}
                empty="The organization has no channel."
                columns={[
                  {
                    key: "name",
                    header: "Channel",
                    cell: (r) => r.name,
                    rowHeader: true,
                    sortValue: (r) => r.name,
                  },
                  { key: "type", header: "Type", cell: (r) => r.type, sortValue: (r) => r.type },
                  {
                    key: "enabled",
                    header: "Enabled",
                    cell: (r) => <StatusPill tone={toneOf(r.enabled)}>{r.enabled}</StatusPill>,
                  },
                  { key: "default", header: "Default", cell: (r) => r.isDefault },
                  { key: "target", header: "Target", cell: (r) => r.target },
                  { key: "delivered", header: "Last delivered", cell: (r) => r.lastDelivered },
                  { key: "failed", header: "Last failed", cell: (r) => r.lastFailed },
                  { key: "failures", header: "Failures in a row", cell: (r) => r.failures },
                  {
                    key: "details",
                    header: "Details",
                    cell: (r) => (
                      <Button
                        size="sm"
                        onClick={() => {
                          setMessage(null);
                          setSelected(r.id);
                        }}
                      >
                        Open {r.name}
                      </Button>
                    ),
                  },
                ]}
              />
            </Card>
            {canManage && (
              <CreateChannel
                context={context}
                onCreated={(created) => {
                  setSigningKey(created.signing_key ?? null);
                  setMessage(
                    created.type === "email"
                      ? `Created ${created.name}. Each recipient's confirmation: ${Object.values(created.confirmations ?? {}).join(", ") || "none"}.`
                      : `Created ${created.name}, sending to ${created.url_origin ?? "-"}.`,
                  );
                  setReloads((n) => n + 1);
                }}
              />
            )}
          </>
        ))}
    </div>
  );
}
