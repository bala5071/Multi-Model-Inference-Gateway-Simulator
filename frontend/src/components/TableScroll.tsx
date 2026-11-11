import type { ReactNode } from "react";

/**
 * The box a wide table scrolls in (docs/53). When the table is wider than the box, a keyboard
 * user must be able to reach it and scroll it (axe's scrollable-region-focusable, WCAG 2.1.1):
 * so the box takes focus (`tabIndex` 0) and is a region named after its table ("<name> table").
 * Every table box of the page is this component; a source check keeps it so.
 */
export function TableScroll({ table, children }: { table: string; children: ReactNode }) {
  return (
    <div className="table-scroll" role="region" aria-label={`${table} table`} tabIndex={0}>
      {children}
    </div>
  );
}
