import { budgetUse } from "../api/derive";
import type { MonitorState } from "../api/store";
import { formatUsd, formatValue } from "../charts/transforms";
import { Meter } from "../components/Meter";
import { Unavailable } from "../components/Unavailable";

const LEVEL_TEXT = { ok: "", warning: "Near its budget", exhausted: "Budget used up" } as const;

export function Applications({ state }: { state: MonitorState }) {
  const snapshot = state.snapshot;
  if (snapshot === null) return <p className="empty">Waiting for the first snapshot…</p>;
  const applications = snapshot.applications.applications;
  if (!snapshot.applications.available || applications === null) {
    return <Unavailable what="Applications: this gateway has no plans configured" />;
  }
  return (
    <>
      <p className="muted lead">
        Spend, tokens and requests are for the current budget period of each application.
        Rejections, timeouts and downgrades are counted since the gateway started.
      </p>
      <div className="table-scroll">
        <table className="data-table applications">
          <thead>
            <tr>
              <th scope="col">Application</th>
              <th scope="col">Plan</th>
              <th scope="col">Spend (USD)</th>
              <th scope="col">Token budget</th>
              <th scope="col">Requests</th>
              <th scope="col">Rejected: budget</th>
              <th scope="col">Rejected: queue full</th>
              <th scope="col">Rejected: rate limit</th>
              <th scope="col">Timeouts</th>
              <th scope="col">Downgrades</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(applications).map(([id, application]) => {
              const use = budgetUse(application);
              return (
                <tr key={id}>
                  <th scope="row">{id}</th>
                  <td>{application.plan}</td>
                  <td>{formatUsd(application.spend_usd)}</td>
                  <td className="budget">
                    <span>
                      {formatValue(application.used_tokens)} of{" "}
                      {formatValue(application.budget_tokens)} tokens (
                      {(use.ratio * 100).toFixed(1)}%)
                    </span>
                    <Meter label={`${id} token budget used`} ratio={use.ratio} level={use.level} />
                    {use.level !== "ok" && <span className="muted">{LEVEL_TEXT[use.level]}</span>}
                  </td>
                  <td>{formatValue(application.requests)}</td>
                  <td>{formatValue(application.rejections_total.budget_exhausted)}</td>
                  <td>{formatValue(application.rejections_total.queue_full)}</td>
                  <td>{formatValue(application.rejections_total.rate_limited)}</td>
                  <td>{formatValue(application.timeout_total)}</td>
                  <td>{formatValue(application.downgrade_total)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}
