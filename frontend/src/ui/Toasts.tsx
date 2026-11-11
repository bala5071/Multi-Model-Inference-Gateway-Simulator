import { CircleAlert, CircleCheck, X } from "lucide-react";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";

/** How long a toast stays before it goes by itself; the person can dismiss it sooner. */
export const TOAST_MS = 8000;

/** A change's result (`done`), or a failure (`failed`: announced at once, `role="alert"`). */
export type ToastTone = "done" | "failed";

interface Toast {
  id: number;
  text: string;
  tone: ToastTone;
}

type Show = (text: string, tone?: ToastTone) => void;

const Context = createContext<Show>(() => undefined);

/**
 * The page's toasts (docs/54): the result of a change, said once, in a polite live region, in
 * the corner, then gone. Never the only place an error is shown (errors stay beside their form;
 * a failure toast only repeats one), and never a secret: what they say is the screen's own
 * sentence about what was done.
 */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const next = useRef(1);
  const timers = useRef(new Map<number, number>());

  const dismiss = useCallback((id: number) => {
    setToasts((now) => now.filter((t) => t.id !== id));
    const timer = timers.current.get(id);
    if (timer !== undefined) window.clearTimeout(timer);
    timers.current.delete(id);
  }, []);

  const show = useCallback(
    (text: string, tone: ToastTone = "done") => {
      const id = next.current++;
      setToasts((now) => [...now, { id, text, tone }]);
      timers.current.set(
        id,
        window.setTimeout(() => {
          dismiss(id);
        }, TOAST_MS),
      );
    },
    [dismiss],
  );

  useEffect(() => {
    const pending = timers.current;
    return () => {
      for (const timer of pending.values()) window.clearTimeout(timer);
    };
  }, []);

  return (
    <Context value={show}>
      {children}
      <div className="toasts" aria-live="polite">
        {toasts.map((toast) => (
          <div
            key={toast.id}
            className={toast.tone === "failed" ? "toast toast-failed" : "toast"}
            role={toast.tone === "failed" ? "alert" : "status"}
          >
            {toast.tone === "failed" ? (
              <CircleAlert aria-hidden="true" focusable="false" size={18} className="toast-icon" />
            ) : (
              <CircleCheck aria-hidden="true" focusable="false" size={18} className="toast-icon" />
            )}
            <span>{toast.text}</span>
            <button
              type="button"
              className="quiet toast-dismiss"
              aria-label="Dismiss"
              onClick={() => {
                dismiss(toast.id);
              }}
            >
              <X aria-hidden="true" focusable="false" size={16} />
            </button>
          </div>
        ))}
      </div>
    </Context>
  );
}

/** Shows a toast. */
export function useToast(): Show {
  return useContext(Context);
}

/** A screen's message, shown as a toast each time it is set (and cleared again before the next). */
export function Toasted({ message }: { message: string | null }) {
  const show = useToast();
  const last = useRef<string | null>(null);
  useEffect(() => {
    if (message !== null && message !== last.current) show(message);
    last.current = message;
  }, [message, show]);
  return null;
}
