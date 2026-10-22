import { useCallback, useEffect, useState } from "react";
import { Outlet, useLocation } from "react-router-dom";
import { Sidebar } from "./Sidebar";
import { useKeyboardShortcut } from "@/hooks/useKeyboardShortcut";
import { useMeta } from "@/api/MetaContext";
import { StatusMessage, Icon } from "@/components";
import "./AppShell.css";

/**
 * The frame every section sits in: the drawer, its trigger, and the one place
 * that tells the document which section is open.
 *
 * `data-section` on <body> is what lets the map own the viewport while every
 * other section scrolls normally, without a component knowing about the others.
 */
export function AppShell() {
  const [navOpen, setNavOpen] = useState(false);
  const location = useLocation();
  const { error, meta } = useMeta();
  const brain = meta?.brain_name;

  const section = location.pathname.split("/")[1] || "map";

  useEffect(() => {
    document.body.dataset.section = section;
    return () => {
      delete document.body.dataset.section;
    };
  }, [section]);

  // The tab says whose brain this is once it has a name of its own.
  useEffect(() => {
    document.title = brain && brain !== "Brain" ? `${brain} · second-brain` : "second-brain";
  }, [brain]);

  useKeyboardShortcut("m", useCallback(() => setNavOpen((v) => !v), []));

  return (
    <>
      <button
        type="button"
        className="ml-burger"
        title="sections (M)"
        aria-label="sections"
        aria-expanded={navOpen}
        onClick={() => setNavOpen((v) => !v)}
      >
        <Icon name="menu" />
      </button>
      <Sidebar open={navOpen} onClose={() => setNavOpen(false)} />
      {error ? (
        <div className="ml-shell__meta-error">
          <StatusMessage tone="error">
            Could not read the server vocabulary ({error}). Editing is hidden until it answers —
            this is not the same as <code>--no-write</code>.
          </StatusMessage>
        </div>
      ) : null}
      <Outlet />
    </>
  );
}
