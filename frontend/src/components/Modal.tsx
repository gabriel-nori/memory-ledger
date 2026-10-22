import { useEffect, useRef } from "react";
import type { ReactNode } from "react";
import { createPortal } from "react-dom";
import { Button } from "./Button";
import "./Modal.css";
import { Icon } from "./Icon";

export interface ModalProps {
  title: ReactNode;
  subtitle?: ReactNode;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  /** Stack depth — a rule form opened on top of a skill form sits at 1. */
  depth?: number;
  width?: number | string;
}

/**
 * A dialog that stacks, traps focus, and closes on Escape.
 *
 * The old page stacked modals by hand (`Create.shell`) and the inner one could
 * not be dismissed with the keyboard. Escape closes the TOP modal only, which
 * is what depth is for.
 */
export function Modal({ title, subtitle, onClose, children, footer, depth = 0, width = 620 }: ModalProps) {
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      // Only the topmost dialog reacts, so Escape peels the stack one at a time.
      const all = document.querySelectorAll("[data-ml-modal]");
      if (all[all.length - 1] === box.current?.parentElement) {
        e.stopPropagation();
        onClose();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  useEffect(() => {
    // Focus lands inside the dialog so the keyboard is already in the form.
    const first = box.current?.querySelector<HTMLElement>(
      "input, textarea, select, button, [tabindex]:not([tabindex='-1'])",
    );
    first?.focus();
  }, []);

  return createPortal(
    <div
      className="ml-modal__scrim"
      data-ml-modal
      style={{ zIndex: 60 + depth }}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={box}
        className="ml-modal"
        role="dialog"
        aria-modal="true"
        aria-label={typeof title === "string" ? title : undefined}
        style={{ width, maxWidth: "calc(100vw - 32px)" }}
      >
        <header className="ml-modal__head">
          <div>
            <h2 className="ml-modal__title">{title}</h2>
            {subtitle ? <p className="ml-modal__sub">{subtitle}</p> : null}
          </div>
          <Button variant="ghost" onClick={onClose} aria-label="close"><Icon name="close" /></Button>
        </header>
        <div className="ml-modal__body">{children}</div>
        {footer ? <footer className="ml-modal__foot">{footer}</footer> : null}
      </div>
    </div>,
    document.body,
  );
}
