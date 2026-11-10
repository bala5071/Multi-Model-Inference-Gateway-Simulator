import type { Capabilities } from "../console/types";
import { itemOf, shownItems, target } from "./navigation";
import { routeHash, type Route } from "./routes";

/** The items the gateway's answer allows, and nothing else (docs/50). */
export function NavBar({ capabilities, route }: { capabilities: Capabilities; route: Route }) {
  const current = itemOf(route);
  return (
    <nav className="console-nav" aria-label="Sections">
      {shownItems(capabilities).map((item) => (
        <a
          key={item.id}
          href={routeHash(target(item, route, capabilities))}
          aria-current={item.id === current ? "page" : undefined}
        >
          {item.label}
        </a>
      ))}
    </nav>
  );
}
