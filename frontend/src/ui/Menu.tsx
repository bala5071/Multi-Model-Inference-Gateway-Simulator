import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";

/**
 * The WAI-ARIA menu button pattern (docs/54, docs/55), for any menu of `count` items: the button
 * opens it (click, Enter, Space, ArrowDown to the first item, ArrowUp to the last); in it, the
 * arrow keys, Home and End move, Escape closes it and gives the focus back to the button, Tab
 * closes it; a press outside closes it. The caller draws the button, the menu and the items and
 * spreads what this returns onto them.
 */
export function useMenuButton(count: number) {
  const [open, setOpen] = useState(false);
  const [focusIndex, setFocusIndex] = useState(0);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const items = useRef<(HTMLButtonElement | null)[]>([]);
  const menuId = useId();

  useEffect(() => {
    if (open) items.current[focusIndex]?.focus();
  }, [open, focusIndex]);

  useEffect(() => {
    if (!open) return;
    const outside = (event: MouseEvent) => {
      const target = event.target as Node;
      if (!menuRef.current?.contains(target) && !buttonRef.current?.contains(target)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", outside);
    return () => {
      document.removeEventListener("mousedown", outside);
    };
  }, [open]);

  function openAt(index: number) {
    setFocusIndex(index);
    setOpen(true);
  }

  function close(returnFocus: boolean) {
    setOpen(false);
    if (returnFocus) buttonRef.current?.focus();
  }

  const buttonProps = {
    ref: buttonRef,
    "aria-haspopup": "menu" as const,
    "aria-expanded": open,
    "aria-controls": open ? menuId : undefined,
    onClick: () => {
      if (open) close(false);
      else openAt(0);
    },
    onKeyDown: (event: KeyboardEvent<HTMLButtonElement>) => {
      if (event.key === "ArrowDown" || event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openAt(0);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        openAt(count - 1);
      }
    },
  };

  const menuProps = {
    ref: menuRef,
    id: menuId,
    role: "menu" as const,
    tabIndex: -1,
    onKeyDown: (event: KeyboardEvent<HTMLDivElement>) => {
      const moves: Record<string, number> = {
        ArrowDown: (focusIndex + 1) % count,
        ArrowUp: (focusIndex - 1 + count) % count,
        Home: 0,
        End: count - 1,
      };
      const next = moves[event.key];
      if (next !== undefined) {
        event.preventDefault();
        setFocusIndex(next);
      } else if (event.key === "Escape") {
        event.preventDefault();
        close(true);
      } else if (event.key === "Tab") {
        close(false);
      }
    },
  };

  /** The props of item `index`: out of the Tab order, its element remembered for the focus. */
  function itemProps(index: number) {
    return {
      ref: (element: HTMLButtonElement | null) => {
        items.current[index] = element;
      },
      tabIndex: -1,
    };
  }

  return { open, close, buttonProps, menuProps, itemProps };
}
