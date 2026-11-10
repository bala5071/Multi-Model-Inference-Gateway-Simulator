import { useState } from "react";
import { orgPath } from "./api";
import type { ConsoleContext } from "./context";
import { useLoad } from "./useLoad";
import type { ApplicationPage } from "./types";

/**
 * Which application: from the organization's list, or typed when the list is not allowed (an
 * application's own admin or viewer names the one it was given). With `all`, "All
 * applications" can be chosen too (`null`).
 */
export function Chooser({
  context,
  selected,
  onChoose,
  all = false,
}: {
  context: ConsoleContext;
  selected: string | null;
  onChoose: (id: string | null) => void;
  all?: boolean;
}) {
  const org = context.user.organization_id;
  const list = useLoad(`apps:${org}`, () =>
    context.api.get<ApplicationPage>(`${orgPath(org)}/applications?limit=100`),
  );
  const [typed, setTyped] = useState(selected ?? "");

  if (list.kind === "loading") return <p className="muted">Loading applications…</p>;
  if (list.kind === "ready") {
    return (
      <label className="chooser">
        Application
        <select
          value={selected ?? ""}
          onChange={(event) => {
            onChoose(event.target.value === "" ? null : event.target.value);
          }}
        >
          <option value="" disabled={!all}>
            {all ? "All applications" : "Choose an application"}
          </option>
          {list.value.applications.map((a) => (
            <option key={a.application_id} value={a.application_id}>
              {a.name} ({a.application_id})
            </option>
          ))}
        </select>
      </label>
    );
  }
  return (
    <form
      className="chooser"
      onSubmit={(event) => {
        event.preventDefault();
        if (typed !== "") onChoose(typed);
      }}
    >
      <label>
        Application id
        <input
          value={typed}
          onChange={(event) => {
            setTyped(event.target.value);
          }}
        />
      </label>
      <button type="submit">Open</button>
    </form>
  );
}
