import { ArrowDown, ArrowUp, ArrowUpDown } from "lucide-react";
import { useState, type ReactNode } from "react";
import { TableScroll } from "../components/TableScroll";
import { SkeletonRows } from "./Skeleton";
import { nextSort, sortRows, type Direction, type SortValue } from "./sort";

export interface Column<R> {
  key: string;
  /** The header's words; a sortable header's button is named by them. */
  header: string;
  cell: (row: R) => ReactNode;
  /** Given, the header is a sort button; the value it sorts by (null last). */
  sortValue?: (row: R) => SortValue;
  /** The row's header cell (`<th scope="row">`), as the screen's first column usually is. */
  rowHeader?: boolean;
}

/** What the sort note under a sortable table says (docs/52, Q9). */
export const SORT_NOTE = "Sorting applies to the rows on this page only.";

/**
 * A table (docs/55): inside the scroll box, its header sticky; the columns given a `sortValue`
 * sort the current page as header buttons with `aria-sort`, said under the table; empty, loading
 * (skeleton rows) and error states. The rows are the screen's; the table computes nothing else.
 */
export function DataTable<R>({
  label,
  columns,
  rows,
  rowKey,
  loading = false,
  error = null,
  empty,
}: {
  label: string;
  columns: readonly Column<R>[];
  rows: readonly R[];
  rowKey: (row: R) => string;
  loading?: boolean;
  error?: string | null;
  empty: ReactNode;
}) {
  const [sort, setSort] = useState<{ key: string; direction: Direction } | null>(null);

  if (error !== null) {
    return (
      <p className="form-error" role="alert">
        {error}
      </p>
    );
  }
  if (!loading && rows.length === 0) return <p className="muted empty-state">{empty}</p>;

  const sorting = columns.find((c) => c.key === sort?.key);
  const shown =
    sort !== null && sorting?.sortValue !== undefined
      ? sortRows(rows, sorting.sortValue, sort.direction)
      : rows;
  const sortable = columns.some((c) => c.sortValue !== undefined);

  return (
    <>
      <TableScroll table={label}>
        <table className="data-table ui-table" aria-label={label} aria-busy={loading || undefined}>
          <thead>
            <tr>
              {columns.map((column) => {
                const direction = sort?.key === column.key ? sort.direction : null;
                return (
                  <th
                    key={column.key}
                    scope="col"
                    aria-sort={column.sortValue === undefined ? undefined : (direction ?? "none")}
                  >
                    {column.sortValue === undefined ? (
                      column.header
                    ) : (
                      <button
                        type="button"
                        className="sort-button"
                        onClick={() => {
                          setSort(nextSort(sort, column.key));
                        }}
                      >
                        {column.header}
                        {direction === "ascending" ? (
                          <ArrowUp aria-hidden="true" focusable="false" size={14} />
                        ) : direction === "descending" ? (
                          <ArrowDown aria-hidden="true" focusable="false" size={14} />
                        ) : (
                          <ArrowUpDown aria-hidden="true" focusable="false" size={14} />
                        )}
                      </button>
                    )}
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <SkeletonRows columns={columns.length} />
            ) : (
              shown.map((row) => (
                <tr key={rowKey(row)}>
                  {columns.map((column) =>
                    column.rowHeader === true ? (
                      <th key={column.key} scope="row">
                        {column.cell(row)}
                      </th>
                    ) : (
                      <td key={column.key}>{column.cell(row)}</td>
                    ),
                  )}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </TableScroll>
      {sortable && !loading && <p className="muted table-note">{SORT_NOTE}</p>}
    </>
  );
}
