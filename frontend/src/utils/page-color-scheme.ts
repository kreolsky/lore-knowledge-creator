/** The colour scheme an html page declares for itself via `<meta name="color-scheme">`. */

/** 'dark' / 'light' when the page declares exactly one scheme; null when it declares
 *  none or both (an adaptive page follows the viewer's theme). */
export function declaredColorScheme(html: string): 'dark' | 'light' | null {
  const doc = new DOMParser().parseFromString(html, 'text/html');
  const value = doc.querySelector('meta[name="color-scheme"]')?.getAttribute('content') ?? '';
  const schemes = new Set(value.toLowerCase().split(/\s+/).filter(v => v === 'dark' || v === 'light'));
  if (schemes.size !== 1) return null;
  return schemes.has('dark') ? 'dark' : 'light';
}
