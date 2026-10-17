import React, { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";

export interface InformationControlProps {
  label: string;
  children: React.ReactNode;
}

/** Shared keyboard-accessible explanation surface for dashboard panels. */
export function InformationControl({
  label,
  children,
}: InformationControlProps): React.JSX.Element {
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const backdrop = useRef<HTMLDivElement>(null);
  const dialog = useRef<HTMLElement>(null);
  const generatedId = useId();
  const dialogId = `information-${generatedId.replaceAll(":", "")}`;
  const headingId = `${dialogId}-heading`;

  const close = () => {
    setOpen(false);
    requestAnimationFrame(() => trigger.current?.focus({ preventScroll: true }));
  };

  useEffect(() => {
    if (!open) return;
    closeButton.current?.focus({ preventScroll: true });
    const inertBackground = new Map<HTMLElement, boolean>();
    for (const child of document.body.children) {
      if (!(child instanceof HTMLElement) || child === backdrop.current) continue;
      inertBackground.set(child, child.hasAttribute("inert"));
      child.setAttribute("inert", "");
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        close();
        return;
      }
      if (event.key !== "Tab") return;
      const surface = dialog.current;
      if (!surface) return;
      const focusable = [...surface.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      )];
      if (!focusable.length) {
        event.preventDefault();
        surface.focus({ preventScroll: true });
        return;
      }
      const first = focusable[0];
      const last = focusable.at(-1)!;
      const active = document.activeElement;
      if (event.shiftKey && (active === first || !surface.contains(active))) {
        event.preventDefault();
        last.focus({ preventScroll: true });
      } else if (!event.shiftKey && (active === last || !surface.contains(active))) {
        event.preventDefault();
        first.focus({ preventScroll: true });
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      for (const [element, wasInert] of inertBackground) {
        if (!wasInert) element.removeAttribute("inert");
      }
    };
  }, [open]);

  return (
    <>
      <button
        ref={trigger}
        className="information-trigger"
        type="button"
        aria-controls={dialogId}
        aria-expanded={open}
        aria-label={`Information about ${label}`}
        onClick={() => setOpen(true)}
      >
        Information
      </button>
      {open
        ? createPortal(
            <div
              ref={backdrop}
              className="information-backdrop"
              onMouseDown={(event) => {
                if (event.target === event.currentTarget) close();
              }}
            >
              <section
                ref={dialog}
                id={dialogId}
                className="information-dialog"
                role="dialog"
                aria-modal="true"
                aria-labelledby={headingId}
                tabIndex={-1}
              >
                <div className="information-heading">
                  <h2 id={headingId}>{label}</h2>
                  <button ref={closeButton} type="button" onClick={close}>
                    Close
                  </button>
                </div>
                <div className="information-content">{children}</div>
              </section>
            </div>,
            document.body,
          )
        : null}
    </>
  );
}
