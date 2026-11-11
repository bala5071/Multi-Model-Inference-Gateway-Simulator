import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { render, screen } from "@testing-library/react";
import { TableScroll } from "./TableScroll";

/** Every .tsx file under src/, read from disk (the tests run in frontend/). */
function sources(dir = join(process.cwd(), "src")): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return sources(path);
    return name.endsWith(".tsx") && !name.endsWith(".test.tsx") ? [path] : [];
  });
}

describe("TableScroll (docs/53)", () => {
  it("is a focusable region named after its table", () => {
    render(
      <TableScroll table="Keys">
        <table aria-label="Keys">
          <tbody>
            <tr>
              <td>1</td>
            </tr>
          </tbody>
        </table>
      </TableScroll>,
    );
    const box = screen.getByRole("region", { name: "Keys table" });
    expect(box).toHaveAttribute("tabindex", "0");
    expect(box).toHaveClass("table-scroll");
    expect(screen.getByRole("table", { name: "Keys" })).toBeInTheDocument();
  });

  it("is the only way the page draws a table's scroll box", () => {
    const bare = sources()
      .filter((path) => !path.endsWith(join("components", "TableScroll.tsx")))
      .filter((path) => readFileSync(path, "utf8").includes("table-scroll"))
      .map((path) => relative(process.cwd(), path));
    expect(bare).toEqual([]);
  });

  it("names every table the page draws (aria-label, aria-labelledby or a caption)", () => {
    const unnamed = sources().flatMap((path) =>
      // A table is named by aria-label or aria-labelledby, or by a <caption> as its first child.
      [...readFileSync(path, "utf8").matchAll(/<table\b[^>]*>\s*(<caption>)?/g)]
        .filter((m) => m[1] === undefined && !/aria-label(ledby)?=/.test(m[0]))
        .map((m) => `${relative(process.cwd(), path)}: ${m[0].trim()}`),
    );
    expect(unnamed).toEqual([]);
  });
});
