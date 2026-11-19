import { useState } from "react";
import { routeHash } from "../shell/routes";
import { ApiError, applicationPath, orgPath } from "./api";
import type { ConsoleContext } from "./context";
import { refusalText } from "./derive";
import { KeyShownOnce } from "./KeyShownOnce";
import { useLoad } from "./useLoad";
import type { ApplicationSummary, IssuedKey, PlanList, PlanTemplate } from "./types";

function planText(plan: PlanTemplate): string {
  const models = plan.allowed_models.join(", ");
  return `${plan.name}: ${models}; ${plan.monthly_token_budget.toLocaleString("en-US")} tokens a month`;
}

function failure(error: unknown): string {
  if (error instanceof ApiError) return refusalText(error.code, error.message);
  return "Something went wrong reading the gateway's answer.";
}

/**
 * Add an application (docs/51), in two steps on one screen: create it from a plan template,
 * then issue its first key, which the gateway asks a password for (step-up). The key is shown
 * once, held in this screen's state only, and forgotten when "I have stored it" is pressed or
 * the screen is left. Whether this session may create one is the gateway's answer: the shell
 * opens this screen only when its capabilities answer allows `applications.create`.
 */
export function AddApplicationScreen({ context }: { context: ConsoleContext }) {
  const org = context.user.organization_id;
  const plans = useLoad(`plans:${org}`, () => context.api.get<PlanList>(`${orgPath(org)}/plans`));
  const [name, setName] = useState("");
  const [plan, setPlan] = useState("");
  const [label, setLabel] = useState("first key");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<ApplicationSummary | null>(null);
  const [key, setKey] = useState<IssuedKey | null>(null);
  const [stored, setStored] = useState(false);

  async function create() {
    setBusy(true);
    setError(null);
    try {
      setCreated(
        await context.api.send<ApplicationSummary>("POST", `${orgPath(org)}/applications`, {
          name,
          plan,
        }),
      );
    } catch (caught) {
      setError(failure(caught));
    } finally {
      setBusy(false);
    }
  }

  async function issue(application: ApplicationSummary) {
    setBusy(true);
    setError(null);
    try {
      const path = `${applicationPath(org, application.application_id)}/keys`;
      setKey(await context.guarded(() => context.api.send<IssuedKey>("POST", path, { label })));
    } catch (caught) {
      setError(failure(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="console-screen panel" aria-labelledby="add-application-title">
      <h2 id="add-application-title">Add application</h2>
      <p>
        <a href={routeHash({ screen: "apps" })}>Back to Applications</a>
      </p>
      <section aria-labelledby="add-step-create">
        <h3 id="add-step-create">1. Create</h3>
        {plans.kind === "loading" && <p className="muted">Loading the plans…</p>}
        {plans.kind === "failed" && (
          <p className="form-error" role="alert">
            {refusalText(plans.error.code, plans.error.message)}
          </p>
        )}
        {plans.kind === "ready" && created === null && (
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void create();
            }}
          >
            <label>
              Name
              <input
                value={name}
                maxLength={100}
                required
                onChange={(event) => {
                  setName(event.target.value);
                }}
              />
            </label>
            <label>
              Plan
              <select
                value={plan}
                required
                onChange={(event) => {
                  setPlan(event.target.value);
                }}
              >
                <option value="" disabled>
                  Choose a plan
                </option>
                {plans.value.plans.map((p) => (
                  <option key={p.name} value={p.name}>
                    {planText(p)}
                  </option>
                ))}
              </select>
            </label>
            <div className="form-actions">
              <button type="submit" disabled={busy || name === "" || plan === ""}>
                Create
              </button>
            </div>
          </form>
        )}
        {created !== null && (
          <p role="status">
            Created <strong>{created.name}</strong> ({created.application_id}) on the {created.plan}{" "}
            plan.
          </p>
        )}
      </section>
      {created !== null && (
        <section aria-labelledby="add-step-key">
          <h3 id="add-step-key">2. First key</h3>
          {key === null && !stored && (
            <form
              onSubmit={(event) => {
                event.preventDefault();
                void issue(created);
              }}
            >
              <p className="muted">
                Issuing a key asks for your password again. The key is shown once.
              </p>
              <label>
                Label
                <input
                  value={label}
                  maxLength={64}
                  required
                  onChange={(event) => {
                    setLabel(event.target.value);
                  }}
                />
              </label>
              <div className="form-actions">
                <button type="submit" disabled={busy || label === ""}>
                  Issue the first key
                </button>
              </div>
            </form>
          )}
          {key !== null && (
            <KeyShownOnce
              value={key.key}
              label={key.label}
              onDone={() => {
                setKey(null);
                setStored(true);
              }}
            />
          )}
          {stored && (
            <p role="status">
              The key is no longer shown.{" "}
              <a href={routeHash({ screen: "keys", applicationId: created.application_id })}>
                Its API keys
              </a>
            </p>
          )}
        </section>
      )}
      {error !== null && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
