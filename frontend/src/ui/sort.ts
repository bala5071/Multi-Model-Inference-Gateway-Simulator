// Sorting a table's current page in the browser (docs/52, Q9): never another page, never the
// gateway's order of pages. Pure, so it is tested alone.

export type SortValue = string | number | null;
export type Direction = "ascending" | "descending";

/**
 * The rows sorted by `value`, stably: equal rows keep their order, and a row without a value
 * (null) goes last in either direction.
 */
export function sortRows<R>(
  rows: readonly R[],
  value: (row: R) => SortValue,
  direction: Direction,
): R[] {
  const sign = direction === "ascending" ? 1 : -1;
  return rows
    .map((row, index) => ({ row, index, key: value(row) }))
    .sort((a, b) => {
      if (a.key === null || b.key === null) {
        if (a.key === b.key) return a.index - b.index;
        return a.key === null ? 1 : -1;
      }
      const order =
        typeof a.key === "number" && typeof b.key === "number"
          ? a.key - b.key
          : String(a.key).localeCompare(String(b.key));
      return order === 0 ? a.index - b.index : sign * order;
    })
    .map((entry) => entry.row);
}

/** The next direction when a sortable header is pressed: ascending first, then it flips. */
export function nextSort(
  current: { key: string; direction: Direction } | null,
  key: string,
): { key: string; direction: Direction } {
  if (current?.key !== key) return { key, direction: "ascending" };
  return { key, direction: current.direction === "ascending" ? "descending" : "ascending" };
}
