import { versionRows } from "./derive";
import type { VersionSummary } from "./types";
import { Button } from "../ui/Button";
import { Card } from "../ui/Card";

/**
 * A settings history as a timeline (docs/55): each version, the fields it changed, who and how,
 * when; and Revert to the previous version where the gateway said this account may write (the
 * same call as before, with its step-up).
 */
export function History({
  versions,
  empty,
  canRevert,
  busy,
  onRevert,
}: {
  versions: readonly VersionSummary[];
  empty: string;
  canRevert: boolean;
  busy: boolean;
  onRevert: () => void;
}) {
  return (
    <Card title="History" label="History">
      {versions.length === 0 ? (
        <p className="muted">{empty}</p>
      ) : (
        <ol className="history" aria-label="Versions">
          {versionRows(versions).map((row) => (
            <li key={row.version}>
              <div>
                <strong>Version {row.version}</strong>: {row.fields}
              </div>
              <div className="history-when">
                By {row.by}, {row.via}, <time>{row.when}</time>
              </div>
            </li>
          ))}
        </ol>
      )}
      {canRevert && (
        <div className="form-actions">
          <Button disabled={busy} onClick={onRevert}>
            Revert to the previous version
          </Button>
        </div>
      )}
    </Card>
  );
}
