import { useEffect, useId, useRef, useState } from "react";
import type { ReactNode } from "react";
import "./Dropdown.css";
import { Icon } from "./Icon";

export interface DropdownProps {
  /** What the closed button says on the left. */
  label: ReactNode;
  /** The summary on the right — `all`, `none`, `4/9`. */
  summary?: ReactNode;
  /** Highlighted button, for a facet that is actually filtering something out. */
  active?: boolean;
  children: ReactNode;
  align?: "left" | "right";
  className?: string;
}

/**
 * A button that opens a panel and closes on outside click or Escape.
 *
 * Four things in the old page reimplemented this (the facet menus, the display
 * menu, the skills Options menu, the relation picker) and only two of them
 * closed on Escape.
 */
export function Dropdown({
  label,
  summary,
  active = false,
  children,
  align = "left",
  className = "",
}: DropdownProps) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const menuId = useId();

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (root.current && !root.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div
      ref={root}
      className={["ml-dd", open ? "is-open" : "", active ? "is-active" : "", className]
        .filter(Boolean)
        .join(" ")}
    >
      <button
        type="button"
        className="ml-dd__btn"
        aria-expanded={open}
        aria-haspopup="true"
        aria-controls={menuId}
        onClick={() => setOpen((v) => !v)}
      >
        <span>{label}</span>
        {summary !== undefined ? <span className="ml-dd__summary">{summary}</span> : null}
        <span className="ml-dd__caret" aria-hidden="true"><Icon name="chevron-down" /></span>
      </button>
      {open ? (
        <div className={`ml-dd__menu ml-dd__menu--${align}`} id={menuId} role="group">
          {children}
        </div>
      ) : null}
    </div>
  );
}
