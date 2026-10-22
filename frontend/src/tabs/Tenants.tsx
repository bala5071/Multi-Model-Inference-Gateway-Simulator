import { budgetUse } from "../api/derive";
import type { MonitorState } from "../api/store";
import { formatUsd, formatValue } from "../charts/transforms";
import { Meter } from "../components/Meter";
import { Unavailable } from "../components/Unavailable";

const LEVEL_TEXT = { ok: "", warning: "Near its budget", exhausted: "Budget used up" } as const;

export function Tenants({ state }: { state: MonitorState }) {
  const snapshot = state.snapshot;
  if (snapshot === null) return <p className="empty">Waiting for the first snapshot…</p>;
  const tenants = snapshot.tenants.tenants;
  if (!snapshot.tenants.available || tenants === null) {
    return <Unavailable what="Tenants: this gateway has no plans configured" />;
  }
  return (
    <>
      <p className="muted lead">
        Spend, tokens and requests are for the current budget period of each tenant. Rejections,
        timeouts and downgrades are counted since the gateway started.
      </p>
      <div className="table-scroll">
        <table className="data-table tenants">
          <thead>
            <tr>
              <th scope="col">Tenant</th>
              <th scope="col">Plan</th>
              <th scope="col">Spend (USD)</th>
              <th scope="col">Token budget</th>
              <th scope="col">Requests</th>
              <th scope="col">Rejected: budget</th>
              <th scope="col">Rejected: queue full</th>
              <th scope="col">Timeouts</th>
              <th scope="col">Downgrades</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(tenants).map(([id, tenant]) => {
              const use = budgetUse(tenant);
              return (
                <tr key={id}>
                  <th scope="row">{id}</th>
                  <td>{tenant.plan}</td>
                  <td>{formatUsd(tenant.spend_usd)}</td>
                  <td className="budget">
                    <span>
                      {formatValue(tenant.used_tokens)} of {formatValue(tenant.budget_tokens)}{" "}
                      tokens ({(use.ratio * 100).toFixed(1)}%)
                    </span>
                    <Meter label={`${id} token budget used`} ratio={use.ratio} level={use.level} />
                    {use.level !== "ok" && <span className="muted">{LEVEL_TEXT[use.level]}</span>}
                  </td>
                  <td>{formatValue(tenant.requests)}</td>
                  <td>{formatValue(tenant.rejections_total.budget_exhausted)}</td>
                  <td>{formatValue(tenant.rejections_total.queue_full)}</td>
                  <td>{formatValue(tenant.timeout_total)}</td>
                  <td>{formatValue(tenant.downgrade_total)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}
