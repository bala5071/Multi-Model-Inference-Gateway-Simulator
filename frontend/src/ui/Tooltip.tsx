import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from "react";

/**
 * A tooltip (docs/52 section 4, docs/55): supplementary words for a control, opened on focus as
 * well as hover, closed by Escape, kept open while the pointer is over it (WCAG 1.4.13). Drawn in
 * the top layer (`popover="manual"`), so no scroll box clips it; placed under its trigger through
 * the CSSOM, never a style attribute (the CSP's style-src 'self'). The trigger is a button, so it
 * takes the focus; the words describe it (`aria-describedby`).
 */
export function Tooltip({
  text,
  children,
  className,
}: {
  text: string;
  children: ReactNode;
  className?: string;
}) {
  const id = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const tip = useRef<HTMLSpanElement>(null);
  const [over, setOver] = useState({ trigger: false, tip: false, focus: false });
  const open = over.trigger || over.tip || over.focus;

  const place = useCallback(() => {
    const t = trigger.current;
    const p = tip.current;
    if (t === null || p === null) return;
    const box = t.getBoundingClientRect();
    p.style.setProperty("top", `${Math.round(box.bottom + 6)}px`);
    p.style.setProperty("left", `${Math.round(Math.max(8, box.left))}px`);
  }, []);

  useEffect(() => {
    const p = tip.current;
    // jsdom has no popover API; the words are in the page (and describe the trigger) either way.
    if (p === null || typeof p.showPopover !== "function") return;
    if (open) {
      place();
      if (!p.matches(":popover-open")) p.showPopover();
    } else if (p.matches(":popover-open")) {
      p.hidePopover();
    }
  }, [open, place]);

  return (
    <>
      <button
        ref={trigger}
        type="button"
        className={className === undefined ? "tooltip-trigger" : `tooltip-trigger ${className}`}
        aria-describedby={id}
        onMouseEnter={() => {
          setOver((o) => ({ ...o, trigger: true }));
        }}
        onMouseLeave={() => {
          setOver((o) => ({ ...o, trigger: false }));
        }}
        onFocus={() => {
          setOver((o) => ({ ...o, focus: true }));
        }}
        onBlur={() => {
          setOver((o) => ({ ...o, focus: false }));
        }}
        onKeyDown={(event) => {
          if (event.key === "Escape") setOver({ trigger: false, tip: false, focus: false });
        }}
      >
        {children}
      </button>
      <span
        ref={tip}
        id={id}
        role="tooltip"
        popover="manual"
        className="tooltip"
        onMouseEnter={() => {
          setOver((o) => ({ ...o, tip: true }));
        }}
        onMouseLeave={() => {
          setOver((o) => ({ ...o, tip: false }));
        }}
      >
        {text}
      </span>
    </>
  );
}
