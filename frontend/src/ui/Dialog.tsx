import { useEffect, useId, useRef, type ReactNode } from "react";
import { Button } from "./Button";

/**
 * A modal dialog on the native `<dialog>` with `showModal()` (docs/52, Q2): the browser moves the
 * focus in, makes the rest of the page inert, closes it on Escape (which calls `onClose`) and
 * gives the focus back when it closes. Its content exists only while it is open.
 */
export function Dialog({
  open,
  title,
  onClose,
  children,
  role,
}: {
  open: boolean;
  title: ReactNode;
  onClose: () => void;
  children: ReactNode;
  role?: "alertdialog";
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();

  useEffect(() => {
    const dialog = ref.current;
    if (dialog === null) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  useEffect(() => {
    const dialog = ref.current;
    return () => {
      if (dialog?.open === true) dialog.close();
    };
  }, []);

  return (
    <dialog
      ref={ref}
      className="ui-dialog"
      role={role}
      aria-labelledby={titleId}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
    >
      {open && (
        <>
          <h3 id={titleId}>{title}</h3>
          {children}
        </>
      )}
    </dialog>
  );
}

/**
 * A confirmation before an action that cannot be undone: it names the thing and the consequence,
 * and Cancel comes first, so it is where the focus starts and what Enter presses; the destructive
 * button is never the default.
 */
export function ConfirmDialog({
  open,
  title,
  children,
  confirmLabel,
  busy = false,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  children: ReactNode;
  confirmLabel: string;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <Dialog open={open} title={title} onClose={onCancel} role="alertdialog">
      <div className="dialog-body">{children}</div>
      <div className="form-actions dialog-actions">
        <Button variant="secondary" onClick={onCancel}>
          Cancel
        </Button>
        <Button variant="danger" loading={busy} loadingText="Working…" onClick={onConfirm}>
          {confirmLabel}
        </Button>
      </div>
    </Dialog>
  );
}
