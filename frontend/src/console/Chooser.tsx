import { useState } from "react";
import { orgPath } from "./api";
import type { ConsoleContext } from "./context";
import { useLoad } from "./useLoad";
import type { ApplicationPage } from "./types";

/**
 * Which application: from the organization's list; when that list is refused, from the
 * applications the gateway's capabilities answer named for this session (an application's own
 * admin or reader); typed only when it named none. With `all`, "All applications" can be chosen
 * too (`null`).
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
  const named = context.capabilities.applications.map((a) => a.application_id);
  const options =
    list.kind === "ready"
      ? list.value.applications.map((a) => ({
          id: a.application_id,
          label: `${a.name} (${a.application_id})`,
        }))
      : named.map((id) => ({ id, label: id }));
  if (list.kind === "ready" || options.length > 0) {
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
          {options.map((option) => (
            <option key={option.id} value={option.id}>
              {option.label}
            </option>
          ))}
          {selected !== null && !options.some((option) => option.id === selected) && (
            <option value={selected}>{selected}</option>
          )}
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
