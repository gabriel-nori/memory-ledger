import { NavLink } from "react-router-dom";
import { useMeta } from "@/api/MetaContext";
import { API_DOCS_URL } from "@/features/apidocs/ApiDocsSection";
import "./Sidebar.css";
import { Icon } from "@/components";
import type { IconName } from "@/components";

export interface SectionLink {
  to: string;
  label: string;
  icon: IconName;
  /** Pinned to the bottom, under the divider. */
  foot?: boolean;
}

export const SECTIONS: SectionLink[] = [
  { to: "/map", label: "Map", icon: "map" },
  { to: "/dashboard", label: "Dashboard", icon: "dashboard" },
  { to: "/tasks", label: "Tasks", icon: "tasks" },
  { to: "/people", label: "People", icon: "people" },
  { to: "/messages", label: "Messages", icon: "message" },
  { to: "/agents", label: "Agents", icon: "sparkle" },
  { to: "/usage", label: "Usage", icon: "chart" },
  { to: "/capabilities", label: "Capabilities", icon: "cube" },
  { to: "/settings", label: "Settings", icon: "sliders", foot: true },
];

export interface SidebarProps {
  open: boolean;
  onClose: () => void;
}

/**
 * The section switcher, as a drawer rather than a top bar.
 *
 * A drawer keeps the sections out of the search bar's way and leaves room for
 * Settings pinned at the bottom.
 */
export function Sidebar({ open, onClose }: SidebarProps) {
  const { meta } = useMeta();
  const brain = meta?.brain_name;
  const main = SECTIONS.filter((s) => !s.foot);
  const foot = SECTIONS.filter((s) => s.foot);

  const item = (s: SectionLink) => (
    <NavLink
      key={s.to}
      to={s.to}
      className={({ isActive }) => `ml-side__link${isActive ? " is-active" : ""}`}
      onClick={onClose}
    >
      <Icon name={s.icon} />
      <span>{s.label}</span>
    </NavLink>
  );

  return (
    <>
      {open ? <div className="ml-side__scrim" onClick={onClose} /> : null}
      <aside className={`ml-side${open ? " is-open" : ""}`} aria-label="sections" aria-hidden={!open}>
        <div className="ml-side__head">
          {brain && brain !== "Brain" ? (
            <span title="second-brain">
              <Icon name="brain" /> <b>{brain}</b>
            </span>
          ) : (
            <span>
              <Icon name="brain" /> second-<b>brain</b>
            </span>
          )}
          <button type="button" className="ml-side__x" onClick={onClose} aria-label="close sections"><Icon name="close" /></button>
        </div>
        <nav className="ml-side__nav">{main.map(item)}</nav>
        <div className="ml-side__foot">
          {/*
            Two links on one row. The row opens the server's Swagger UI framed
            inside the app, so reading the API never leaves the brain. The arrow
            is a real navigation to the same page in a tab of its own. They are
            siblings, not nested: a link inside a link is invalid HTML, and the
            arrow's click must not also trigger the route change.
          */}
          <div className="ml-side__row">
            <NavLink
              to="/api-docs"
              className={({ isActive }) =>
                `ml-side__link ml-side__link--docs${isActive ? " is-active" : ""}`
              }
              onClick={onClose}
            >
              <Icon name="book" />
              <span>API docs</span>
            </NavLink>
            <a
              className="ml-side__ext"
              href={API_DOCS_URL}
              target="_blank"
              rel="noreferrer"
              title="open the API docs in a new tab"
              aria-label="open the API docs in a new tab"
              onClick={onClose}
            >
              <Icon name="external" />
            </a>
          </div>
          {foot.map(item)}
        </div>
      </aside>
    </>
  );
}
