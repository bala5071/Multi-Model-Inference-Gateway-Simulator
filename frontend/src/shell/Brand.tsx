/** The product mark (an inline SVG written here, docs/54) and the page's one level-1 heading. */
export function BrandMark({ size = 24 }: { size?: number }) {
  return (
    <svg
      className="brand-mark"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      aria-hidden="true"
      focusable="false"
    >
      <rect x="1" y="1" width="22" height="22" rx="6" className="brand-mark-tile" />
      <path d="M7 12h10M12 7v10" className="brand-mark-cross" />
    </svg>
  );
}

export function Brand({ collapsed = false }: { collapsed?: boolean }) {
  return (
    <div className="brand">
      <BrandMark />
      <h1 className={collapsed ? "visually-hidden" : undefined}>Gateway</h1>
    </div>
  );
}
