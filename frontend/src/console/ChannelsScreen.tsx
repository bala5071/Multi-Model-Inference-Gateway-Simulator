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
        <label>
          Type
          <select
            value={type}
            onChange={(event) => {
              setType(event.target.value);
            }}
          >
            <option value="webhook">webhook</option>
            <option value="email">email</option>
          </select>
        </label>
        <label>
          Name
          <input
            value={name}
            onChange={(event) => {
              setName(event.target.value);
            }}
          />
        </label>
        {type === "webhook" ? (
          <label>
            Webhook URL
            <input
              value={url}
              autoComplete="off"
              spellCheck={false}
              onChange={(event) => {
                setUrl(event.target.value);
              }}
            />
          </label>
        ) : (
          <label>
            Recipients (one address a line)
            <textarea
              value={recipients}
              rows={3}
              onChange={(event) => {
                setRecipients(event.target.value);
              }}
            />
          </label>
        )}
        <label className="inline">
          <input
            type="checkbox"
            checked={isDefault}
            onChange={(event) => {
              setIsDefault(event.target.checked);
            }}
          />
          An organization default (rules sending to the organization&apos;s defaults use it)
        </label>
        {error !== null && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <div className="form-actions">
          <button type="submit" disabled={busy}>
            Create channel
          </button>
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
      <label>
        Code for {address}
        <input
          value={code}
          autoComplete="one-time-code"
          spellCheck={false}
          onChange={(event) => {
            setCode(event.target.value);
          }}
        />
      </label>
      <button type="submit" disabled={busy || code === ""}>
        Confirm
      </button>
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
        <button type="button" onClick={onClose}>
          Back to the list
        </button>
      </div>
      {message !== null && (
        <p className="notice" role="status">
          {message}
        </p>
      )}
      {error !== null && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
      {loaded.kind === "loading" && <p className="muted">Loading…</p>}
      {loaded.kind === "failed" && (
        <p className="form-error" role="alert">
          {refusalText(loaded.error.code, loaded.error.message)}
        </p>
      )}
      {loaded.kind === "ready" && (
        <>
          <h3>{loaded.value.channel.name}</h3>
          {channelRows([loaded.value.channel]).map((row) => (
            <dl className="facts" key={row.id}>
              <div>
                <dt>Type</dt>
                <dd>{row.type}</dd>
              </div>
              <div>
                <dt>Enabled</dt>
                <dd>{row.enabled}</dd>
              </div>
              <div>
                <dt>Default</dt>
                <dd>{row.isDefault}</dd>
              </div>
              <div>
                <dt>Target</dt>
                <dd>{row.target}</dd>
              </div>
              <div>
                <dt>Last delivered</dt>
                <dd>{row.lastDelivered}</dd>
              </div>
              <div>
                <dt>Last failed</dt>
                <dd>{row.lastFailed}</dd>
              </div>
              <div>
                <dt>Failures in a row</dt>
                <dd>{row.failures}</dd>
              </div>
            </dl>
          ))}
          {canManage && (
            <div className="form-actions">
              <button
                type="button"
                disabled={busy}
                onClick={() => {
                  void act(
                    () => send<TestSend>("POST", "/test"),
                    (answer) => `Test send: ${answer.outcome}.`,
                  );
                }}
              >
                Test send
              </button>
              {loaded.value.channel.enabled && (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => {
                    void act(
                      () => send("POST", "/disable"),
                      () => "Disabled.",
                    );
                  }}
                >
                  Disable
                </button>
              )}
              {deleting ? (
                <>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => {
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
                            setDeleting(false);
                          },
                        );
                    }}
                  >
                    Delete it
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      setDeleting(false);
                    }}
                  >
                    Keep it
                  </button>
                </>
              ) : (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => {
                    setDeleting(true);
                  }}
                >
                  Delete
                </button>
              )}
            </div>
          )}
          {loaded.value.channel.type === "email" && (
            <>
              <h4>Recipients</h4>
              <p className="muted">
                Nothing is sent to an address until it is verified with the code mailed to it.
              </p>
              <div className="table-scroll">
                <table className="data-table" aria-label="Recipients">
                  <thead>
                    <tr>
                      <th scope="col">Address</th>
                      <th scope="col">State</th>
                      <th scope="col">Added</th>
                      <th scope="col">Verified</th>
                      <th scope="col">Code valid until</th>
                      <th scope="col">Change</th>
                    </tr>
                  </thead>
                  <tbody>
                    {recipientRows(loaded.value.channel.recipients ?? []).map((r) => (
                      <tr key={r.id}>
                        <th scope="row">{r.address}</th>
                        <td>{r.state}</td>
                        <td>{r.added}</td>
                        <td>{r.verified}</td>
                        <td>{r.unverified ? r.expires : "-"}</td>
                        <td>
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
                                <button
                                  type="button"
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
                                </button>
                              </>
                            )}
                            {canManage && (
                              <button
                                type="button"
                                disabled={busy}
                                onClick={() => {
                                  void act(
                                    () => send("DELETE", `/recipients/${encodeURIComponent(r.id)}`),
                                    () => `Removed ${r.address}.`,
                                  );
                                }}
                              >
                                Remove {r.address}
                              </button>
                            )}
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
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
                  <label>
                    Add a recipient
                    <input
                      value={adding}
                      onChange={(event) => {
                        setAdding(event.target.value);
                      }}
                    />
                  </label>
                  <button type="submit" disabled={busy || adding === ""}>
                    Add
                  </button>
                </form>
              )}
            </>
          )}
          <h4>Deliveries</h4>
          {loaded.value.deliveries.deliveries.length === 0 ? (
            <p className="muted">Nothing delivered yet.</p>
          ) : (
            <div className="table-scroll">
              <table className="data-table" aria-label="Deliveries">
                <thead>
                  <tr>
                    <th scope="col">Notification</th>
                    <th scope="col">State</th>
                    <th scope="col">Attempts</th>
                    <th scope="col">Last code</th>
                    <th scope="col">Next attempt</th>
                    <th scope="col">Updated</th>
                  </tr>
                </thead>
                <tbody>
                  {deliveryRows(loaded.value.deliveries.deliveries).map((d) => (
                    <tr key={d.id}>
                      <td>{d.notification}</td>
                      <td>{d.state}</td>
                      <td>{d.attempts}</td>
                      <td>{d.lastCode}</td>
                      <td>{d.next}</td>
                      <td>{d.updated}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
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
      {message !== null && (
        <p className="notice" role="status">
          {message}
        </p>
      )}
      {signingKey !== null && (
        <SigningKey
          value={signingKey}
          onDone={() => {
            setSigningKey(null);
          }}
        />
      )}
      {loaded.kind === "loading" && <p className="muted">Loading…</p>}
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
            <section className="panel" aria-label="Channel list">
              <h3>Channel list</h3>
              {loaded.value.channels.length === 0 ? (
                <p className="muted">The organization has no channel.</p>
              ) : (
                <div className="table-scroll">
                  <table className="data-table">
                    <thead>
                      <tr>
                        <th scope="col">Channel</th>
                        <th scope="col">Type</th>
                        <th scope="col">Enabled</th>
                        <th scope="col">Default</th>
                        <th scope="col">Target</th>
                        <th scope="col">Last delivered</th>
                        <th scope="col">Last failed</th>
                        <th scope="col">Failures in a row</th>
                        <th scope="col">Details</th>
                      </tr>
                    </thead>
                    <tbody>
                      {channelRows(loaded.value.channels).map((row) => (
                        <tr key={row.id}>
                          <th scope="row">{row.name}</th>
                          <td>{row.type}</td>
                          <td>{row.enabled}</td>
                          <td>{row.isDefault}</td>
                          <td>{row.target}</td>
                          <td>{row.lastDelivered}</td>
                          <td>{row.lastFailed}</td>
                          <td>{row.failures}</td>
                          <td>
                            <button
                              type="button"
                              onClick={() => {
                                setMessage(null);
                                setSelected(row.id);
                              }}
                            >
                              Open {row.name}
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>
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
