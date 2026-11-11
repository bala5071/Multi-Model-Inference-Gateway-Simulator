// WCAG 2 contrast, and the token file read into its themes (docs/53). Pure: used by the
// contrast test only, never by the page.

/** The relative luminance of a #rrggbb colour (WCAG 2.x). */
export function luminance(hex: string): number {
  const match = /^#([0-9a-f]{6})$/i.exec(hex);
  if (match?.[1] === undefined) throw new Error(`not a #rrggbb colour: ${hex}`);
  const value = match[1];
  const channel = (i: number) => {
    const c = parseInt(value.slice(i, i + 2), 16) / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel(0) + 0.7152 * channel(2) + 0.0722 * channel(4);
}

/** The contrast ratio of two colours, 1 to 21. */
export function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x) as [number, number];
  return (hi + 0.05) / (lo + 0.05);
}

export type Tokens = Record<string, string>;

/** The custom properties declared in the first block opened by `selector`. */
export function block(css: string, selector: string): Tokens {
  const start = css.indexOf(`${selector} {`);
  if (start < 0) throw new Error(`no block ${selector}`);
  const body = css.slice(start + selector.length + 2, css.indexOf("}", start));
  const found: Tokens = {};
  for (const [, name, value] of body.matchAll(/--([a-z0-9-]+):\s*([^;]+);/g)) {
    if (name !== undefined && value !== undefined) found[name] = value.trim();
  }
  return found;
}

/** The light theme, and the dark one (the light values with the dark block's on top). */
export function themes(css: string): { light: Tokens; dark: Tokens; darkBeforeScript: Tokens } {
  const light = block(css, ":root");
  return {
    light,
    dark: { ...light, ...block(css, ':root[data-theme="dark"]') },
    darkBeforeScript: { ...light, ...block(css, ":root:not([data-theme])") },
  };
}
