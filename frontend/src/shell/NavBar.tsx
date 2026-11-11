import { PanelLeftClose, PanelLeftOpen } from "lucide-react";
import type { ReactNode } from "react";
import type { Capabilities } from "../console/types";
import { APP_ICON, ITEM_ICONS } from "./icons";
import { appLinks, itemOf, shownItems, target, type NavGroup, type NavItem } from "./navigation";
import { routeHash, type Route } from "./routes";

const GROUPS: readonly (NavGroup | null)[] = [null, "Organization", "Application", "Platform"];

/**
 * The items the gateway's answer allows, and nothing else (docs/50), grouped by level
 * (docs/54). Each link keeps its label as its name, visible or (collapsed) visually hidden with
 * a tooltip, so the navigation reads the same whichever way it is drawn.
 */
export function NavList({
  capabilities,
  route,
  collapsed = false,
  onFollow,
}: {
  capabilities: Capabilities;
  route: Route;
  collapsed?: boolean;
  onFollow?: () => void;
}) {
  // On an application's page, the Applications item is where the session is (docs/56).
  const current = route.screen === "app" ? "apps" : itemOf(route);
  const shown = shownItems(capabilities);
  const own = appLinks(capabilities);
  return (
    <nav className="sidebar-nav" aria-label="Sections">
      {GROUPS.map((group) => {
        const items = shown.filter((item) => item.group === group);
        if (group === "Application" && own.length > 0) {
          return (
            <div key={group} className="nav-group">
              <div className={collapsed ? "visually-hidden" : "nav-group-label"}>{group}</div>
              <ul>
                {own.map((id) => (
                  <li key={id}>
                    <a
                      href={routeHash({
                        screen: "app",
                        applicationId: id,
                        tab: "overview",
                        incidentId: null,
                      })}
                      aria-current={
                        route.screen === "app" && route.applicationId === id ? "page" : undefined
                      }
                      title={collapsed ? id : undefined}
                      onClick={onFollow}
                    >
                      <APP_ICON aria-hidden="true" focusable="false" size={18} />
                      <span className={collapsed ? "visually-hidden" : undefined}>{id}</span>
                    </a>
                  </li>
                ))}
              </ul>
            </div>
          );
        }
        if (items.length === 0) return null;
        return (
          <div key={group ?? "top"} className="nav-group">
            {group !== null && (
              <div className={collapsed ? "visually-hidden" : "nav-group-label"}>{group}</div>
            )}
            <ul>
              {items.map((item: NavItem) => {
                const Icon = ITEM_ICONS[item.id];
                return (
                  <li key={item.id}>
                    <a
                      href={routeHash(target(item, route))}
                      aria-current={item.id === current ? "page" : undefined}
                      title={collapsed ? item.label : undefined}
                      onClick={onFollow}
                    >
                      <Icon aria-hidden="true" focusable="false" size={18} />
                      <span className={collapsed ? "visually-hidden" : undefined}>
                        {item.label}
                      </span>
                    </a>
                  </li>
                );
              })}
            </ul>
          </div>
        );
      })}
    </nav>
  );
}

/** The left sidebar: the mark, the navigation, and the button that narrows it to icons. */
export function Sidebar({
  capabilities,
  route,
  collapsed,
  onToggle,
  brand,
}: {
  capabilities: Capabilities;
  route: Route;
  collapsed: boolean;
  onToggle: () => void;
  brand: ReactNode;
}) {
  const Toggle = collapsed ? PanelLeftOpen : PanelLeftClose;
  return (
    <aside id="sidebar" className="sidebar" aria-label="Sidebar">
      {brand}
      <NavList capabilities={capabilities} route={route} collapsed={collapsed} />
      <button
        type="button"
        className="sidebar-toggle quiet"
        aria-controls="sidebar"
        aria-expanded={!collapsed}
        onClick={onToggle}
      >
        <Toggle aria-hidden="true" focusable="false" size={18} />
        <span className={collapsed ? "visually-hidden" : undefined}>
          {collapsed ? "Expand the sidebar" : "Collapse the sidebar"}
        </span>
      </button>
    </aside>
  );
}
