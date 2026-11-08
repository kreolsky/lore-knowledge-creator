/** Resolves any CSS color to sRGB channels through the browser, for the native picker's hex value and the WCAG contrast warning. */

type Rgb = [number, number, number];

/** WHY: the browser is the only complete CSS color parser (named, hsl, color-mix, …);
 * a detached probe element's computed `color` is always `rgb()`/`rgba()`. */
export function resolveRgb(value: string): Rgb | null {
  const probe = document.createElement('span');
  probe.style.color = value;
  if (!probe.style.color) return null;
  document.body.appendChild(probe);
  const computed = getComputedStyle(probe).color;
  probe.remove();
  const m = computed.match(/[\d.]+/g);
  return m && m.length >= 3 ? [Number(m[0]), Number(m[1]), Number(m[2])] : null;
}

export function toHex(value: string): string {
  const rgb = resolveRgb(value);
  if (!rgb) return '#000000';
  return `#${rgb.map(c => Math.round(c).toString(16).padStart(2, '0')).join('')}`;
}

function luminance([r, g, b]: Rgb): number {
  const lin = (c: number) => { const s = c / 255; return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4; };
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

/** WCAG 2.x contrast ratio between two CSS colors, or null if either does not resolve. */
export function contrastRatio(a: string, b: string): number | null {
  const ra = resolveRgb(a);
  const rb = resolveRgb(b);
  if (!ra || !rb) return null;
  const [hi, lo] = [luminance(ra), luminance(rb)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}
