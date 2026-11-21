/** Image size suffix (`![alt|WIDTHxHEIGHT](url)`) grammar — a leaf module, importable without the widget layer. */

/** The size-suffix grammar shared by parseSizeFromAlt, setImageSize and the ImageToolbar's size field. */
export const SIZE_SUFFIX_RE = /^(\d+%?)(?:x(\d+%?))?$/;

/** Parses `|WIDTHxHEIGHT` from the alt text of an image node. */
export function parseSizeFromAlt(alt: string): { width?: string; height?: string } {
  const pipeIdx = alt.lastIndexOf('|');
  if (pipeIdx === -1) return {};
  const m = SIZE_SUFFIX_RE.exec(alt.slice(pipeIdx + 1).trim());
  if (!m) return {};
  const toCSS = (v: string) => v.endsWith('%') ? v : `${v}px`;
  return { width: toCSS(m[1]), height: m[2] ? toCSS(m[2]) : undefined };
}
