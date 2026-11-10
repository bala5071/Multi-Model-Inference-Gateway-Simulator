import { useEffect, useState } from "react";
import { evaluatorBanner } from "./alerts";
import type { Api } from "./api";
import type { Health } from "./types";

/** How often the banner asks again, while an alert page is open. */
export const HEALTH_EVERY_MS = 30_000;

/**
 * The banner of the plan's section 11.6, on every alert page while `/healthz` says the
 * evaluator is `stale` or `unknown`, or cannot be read. It reads `/healthz` when the page
 * opens and every 30 s while it stays open; it decides nothing else.
 */
export function EvaluatorBanner({ api }: { api: Api }) {
  const [seen, setSeen] = useState<{ health: Health | null; failed: boolean }>({
    health: null,
    failed: false,
  });

  useEffect(() => {
    let current = true;
    const read = () => {
      api.get<Health>("/healthz").then(
        (health) => {
          if (current) setSeen({ health, failed: false });
        },
        () => {
          if (current) setSeen({ health: null, failed: true });
        },
      );
    };
    read();
    const timer = window.setInterval(read, HEALTH_EVERY_MS);
    return () => {
      current = false;
      window.clearInterval(timer);
    };
  }, [api]);

  const banner = evaluatorBanner(seen.health, seen.failed);
  if (banner === null) return null;
  return (
    <div className="evaluator-banner" role="alert" aria-label="Alert evaluation">
      <strong>Alert evaluation: {banner.state}.</strong> {banner.text}
    </div>
  );
}
