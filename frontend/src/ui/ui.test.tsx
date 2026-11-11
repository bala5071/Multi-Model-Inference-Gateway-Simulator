import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { Button } from "./Button";
import { StatTile, StatTiles } from "./Card";
import { Chips } from "./Chips";
import { DataTable, SORT_NOTE } from "./DataTable";
import { ConfirmDialog, Dialog } from "./Dialog";
import { NumberField, TextField } from "./Field";
import { nextSort, sortRows } from "./sort";
import { StatusPill, toneOf } from "./StatusPill";
import { TabPanel, Tabs } from "./Tabs";
import { ToastProvider, useToast } from "./Toasts";
import { Toggle } from "./Toggle";
import { Tooltip } from "./Tooltip";

describe("sortRows (docs/55)", () => {
  const rows = [
    { n: "b", v: 2 },
    { n: "a", v: null },
    { n: "c", v: 1 },
    { n: "d", v: 2 },
  ];
  it("sorts the page by a value, stably, a row without one last in either direction", () => {
    expect(sortRows(rows, (r) => r.v, "ascending").map((r) => r.n)).toEqual(["c", "b", "d", "a"]);
    expect(sortRows(rows, (r) => r.v, "descending").map((r) => r.n)).toEqual(["b", "d", "c", "a"]);
    expect(sortRows(rows, (r) => r.n, "descending").map((r) => r.n)).toEqual(["d", "c", "b", "a"]);
  });
  it("goes ascending first, then flips; another column starts ascending", () => {
    expect(nextSort(null, "a")).toEqual({ key: "a", direction: "ascending" });
    expect(nextSort({ key: "a", direction: "ascending" }, "a")).toEqual({
      key: "a",
      direction: "descending",
    });
    expect(nextSort({ key: "a", direction: "descending" }, "b")).toEqual({
      key: "b",
      direction: "ascending",
    });
  });
});

describe("Button", () => {
  it("draws its role and, while busy, keeps its label and says the loading words", () => {
    const { rerender } = render(<Button variant="danger">Revoke</Button>);
    const button = screen.getByRole("button", { name: "Revoke" });
    expect(button).toHaveClass("button", "danger");
    expect(button).toHaveAttribute("type", "button");
    rerender(
      <Button variant="danger" loading loadingText="Working…">
        Revoke
      </Button>,
    );
    const busy = screen.getByRole("button");
    expect(busy).toBeDisabled();
    expect(busy).toHaveAttribute("aria-busy", "true");
    expect(busy).toHaveTextContent("Revoke");
    expect(within(busy).getByText("Working…")).toHaveClass("visually-hidden");
  });
});

describe("fields", () => {
  it("ties the label, the hint and the gateway's error to the field", () => {
    render(<TextField label="Name" hint="As people know it." error="Taken." />);
    const field = screen.getByRole("textbox", { name: "Name" });
    expect(field).toHaveAttribute("aria-invalid", "true");
    expect(field).toHaveAccessibleDescription("As people know it. Taken.");
  });
  it("reads a number with its unit", () => {
    render(<NumberField label="Timeout" unit="ms" value="10" onChange={() => undefined} />);
    expect(screen.getByRole("spinbutton", { name: "Timeout" })).toHaveAccessibleDescription("ms");
  });
});

describe("Toggle", () => {
  it("is a switch named by its label", async () => {
    const seen: boolean[] = [];
    render(<Toggle label="Retry" checked={false} onChange={(v) => seen.push(v)} />);
    await userEvent.click(screen.getByRole("switch", { name: "Retry" }));
    expect(seen).toEqual([true]);
  });
});

describe("Chips", () => {
  function Harness() {
    const [values, setValues] = useState(["economy"]);
    return <Chips label="allowed_tiers" values={values} onChange={setValues} />;
  }
  it("adds a value with Enter or Add and removes one by its own named button", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const list = screen.getByRole("list", { name: "allowed_tiers: values" });
    await user.type(
      screen.getByRole("textbox", { name: "Add to allowed_tiers" }),
      "standard{Enter}",
    );
    await user.type(screen.getByRole("textbox", { name: "Add to allowed_tiers" }), "premium");
    await user.click(screen.getByRole("button", { name: "Add" }));
    expect(
      within(list)
        .getAllByRole("listitem")
        .map((li) => li.textContent),
    ).toEqual(["economy", "standard", "premium"]);
    await user.click(screen.getByRole("button", { name: "Remove standard from allowed_tiers" }));
    expect(within(list).getAllByRole("listitem")).toHaveLength(2);
  });
});

describe("Tabs", () => {
  function Harness() {
    const [tab, setTab] = useState<"a" | "b" | "c">("a");
    return (
      <>
        <Tabs
          label="State"
          prefix="t"
          tabs={[
            { id: "a", label: "A" },
            { id: "b", label: "B" },
            { id: "c", label: "C" },
          ]}
          selected={tab}
          onSelect={setTab}
        />
        <TabPanel prefix="t" selected={tab}>
          shown {tab}
        </TabPanel>
      </>
    );
  }
  it("keeps one tab in the Tab order and moves with the arrows, Home and End", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const a = screen.getByRole("tab", { name: "A" });
    expect(a).toHaveAttribute("tabindex", "0");
    expect(screen.getByRole("tab", { name: "B" })).toHaveAttribute("tabindex", "-1");
    a.focus();
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "B" })).toHaveFocus();
    expect(screen.getByRole("tab", { name: "B" })).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{End}");
    expect(screen.getByRole("tab", { name: "C" })).toHaveFocus();
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "A" })).toHaveFocus();
    await user.keyboard("{ArrowLeft}{Home}");
    expect(screen.getByRole("tabpanel", { name: "A" })).toHaveTextContent("shown a");
  });
});

describe("DataTable", () => {
  const rows = [
    { id: "1", name: "beta", n: 2 },
    { id: "2", name: "alpha", n: 3 },
  ];
  const columns = [
    { key: "name", header: "Name", cell: (r: (typeof rows)[0]) => r.name, rowHeader: true },
    {
      key: "n",
      header: "Count",
      cell: (r: (typeof rows)[0]) => r.n,
      sortValue: (r: (typeof rows)[0]) => r.n,
    },
  ];
  it("sorts the current page from a header button with aria-sort, and says so", async () => {
    const user = userEvent.setup();
    render(
      <DataTable label="Things" rows={rows} rowKey={(r) => r.id} columns={columns} empty="None." />,
    );
    const header = screen.getByRole("columnheader", { name: "Count" });
    expect(header).toHaveAttribute("aria-sort", "none");
    expect(screen.getByRole("columnheader", { name: "Name" })).not.toHaveAttribute("aria-sort");
    expect(screen.getByText(SORT_NOTE)).toBeInTheDocument();
    await user.click(within(header).getByRole("button", { name: "Count" }));
    expect(header).toHaveAttribute("aria-sort", "ascending");
    expect(screen.getAllByRole("rowheader").map((c) => c.textContent)).toEqual(["beta", "alpha"]);
    await user.click(within(header).getByRole("button", { name: "Count" }));
    expect(header).toHaveAttribute("aria-sort", "descending");
    expect(screen.getAllByRole("rowheader").map((c) => c.textContent)).toEqual(["alpha", "beta"]);
  });
  it("says empty, loading and error in words", () => {
    const { rerender } = render(
      <DataTable label="Things" rows={[]} rowKey={(r) => r.id} columns={columns} empty="None." />,
    );
    expect(screen.getByText("None.")).toBeInTheDocument();
    rerender(
      <DataTable
        label="Things"
        rows={[]}
        rowKey={(r) => r.id}
        columns={columns}
        empty="None."
        loading
      />,
    );
    expect(screen.getByRole("table", { name: "Things" })).toHaveAttribute("aria-busy", "true");
    rerender(
      <DataTable
        label="Things"
        rows={rows}
        rowKey={(r) => r.id}
        columns={columns}
        empty="None."
        error="Not permitted, or not found."
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Not permitted, or not found.");
    expect(screen.queryByRole("table")).toBeNull();
  });
});

describe("pills and tiles", () => {
  it("says the state's word beside its colour", () => {
    render(<StatusPill tone={toneOf("firing")}>firing</StatusPill>);
    expect(screen.getByText("firing")).toHaveClass("pill", "pill-bad");
    expect(toneOf("resolved")).toBe("ok");
    expect(toneOf("something new")).toBe("info");
  });
  it("draws a tile as a term and its value", () => {
    render(
      <StatTiles>
        <StatTile label="Version" value={3} note="current" />
      </StatTiles>,
    );
    expect(screen.getByRole("term")).toHaveTextContent("Version");
    expect(screen.getByRole("definition")).toHaveTextContent("3current");
  });
});

describe("dialogs", () => {
  it("opens modally, and Escape (the cancel event) asks the caller to close", () => {
    const closed: string[] = [];
    render(
      <Dialog open title="A question" onClose={() => closed.push("closed")}>
        <p>Body</p>
      </Dialog>,
    );
    const dialog = screen.getByRole("dialog", { name: "A question" });
    expect(dialog).toHaveAttribute("open");
    fireEvent(dialog, new Event("cancel", { cancelable: true }));
    expect(closed).toEqual(["closed"]);
  });
  it("puts Cancel first in a confirmation and names the destructive action", async () => {
    const seen: string[] = [];
    render(
      <ConfirmDialog
        open
        title="Revoke a key"
        confirmLabel="Revoke"
        onConfirm={() => seen.push("confirm")}
        onCancel={() => seen.push("cancel")}
      >
        <p>Revoke k1? This cannot be undone.</p>
      </ConfirmDialog>,
    );
    const dialog = screen.getByRole("alertdialog", { name: "Revoke a key" });
    const buttons = within(dialog).getAllByRole("button");
    expect(buttons.map((b) => b.textContent)).toEqual(["Cancel", "Revoke"]);
    expect(buttons[1]).toHaveClass("danger");
    await userEvent.click(buttons[0] as HTMLElement);
    await userEvent.click(buttons[1] as HTMLElement);
    expect(seen).toEqual(["cancel", "confirm"]);
  });
  it("draws nothing inside while closed", () => {
    render(
      <Dialog open={false} title="Hidden" onClose={() => undefined}>
        <p>Body</p>
      </Dialog>,
    );
    expect(screen.queryByText("Body")).toBeNull();
  });
});

describe("toasts and tooltips", () => {
  function Shout() {
    const show = useToast();
    return (
      <>
        <button
          type="button"
          onClick={() => {
            show("Saved.");
          }}
        >
          ok
        </button>
        <button
          type="button"
          onClick={() => {
            show("Not saved.", "failed");
          }}
        >
          fail
        </button>
      </>
    );
  }
  it("says a result as a status and a failure as an alert", async () => {
    render(
      <ToastProvider>
        <Shout />
      </ToastProvider>,
    );
    await userEvent.click(screen.getByRole("button", { name: "ok" }));
    await userEvent.click(screen.getByRole("button", { name: "fail" }));
    expect(screen.getByRole("status")).toHaveTextContent("Saved.");
    expect(screen.getByRole("alert")).toHaveTextContent("Not saved.");
  });
  it("describes its trigger, which takes the focus", () => {
    render(<Tooltip text="Fewer than 20 requests.">-</Tooltip>);
    expect(screen.getByRole("button", { name: "-" })).toHaveAccessibleDescription(
      "Fewer than 20 requests.",
    );
  });
});
