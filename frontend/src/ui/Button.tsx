import { LoaderCircle } from "lucide-react";
import type { ButtonHTMLAttributes } from "react";

export type ButtonVariant = "primary" | "secondary" | "danger" | "quiet";

export type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant;
  size?: "sm" | "md";
  /** Busy: the label stays (so the width does), hidden, and screen readers hear `loadingText`. */
  loading?: boolean;
  loadingText?: string;
};

/**
 * A button by role (docs/52 section 4, docs/55): primary for the one action a screen is for,
 * secondary for the others, danger for what cannot be undone, quiet for the small ones.
 */
export function Button({
  variant = "secondary",
  size = "md",
  loading = false,
  loadingText = "Saving…",
  className,
  children,
  disabled,
  type = "button",
  ...rest
}: ButtonProps) {
  const classes = [
    "button",
    variant,
    size === "sm" ? "small" : null,
    loading ? "loading" : null,
    className,
  ]
    .filter((c) => c !== null && c !== undefined && c !== "")
    .join(" ");
  return (
    <button
      type={type}
      className={classes}
      disabled={disabled === true || loading}
      aria-busy={loading ? true : undefined}
      {...rest}
    >
      <span className={loading ? "button-label invisible" : "button-label"}>{children}</span>
      {loading && (
        <>
          <LoaderCircle
            aria-hidden="true"
            focusable="false"
            size={16}
            className="button-spinner spin"
          />
          <span className="visually-hidden">{loadingText}</span>
        </>
      )}
    </button>
  );
}
