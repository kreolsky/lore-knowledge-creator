/**
 * UTF-16 → Unicode code-point offset conversion.
 *
 * CM6 (and the whole JS string model) indexes text in UTF-16 code units; the
 * backend `_resolve_edit_range` / `_splice_edit` and the pinned-region wire shape
 * use Unicode CODE POINTS (Python `str`). A lone surrogate (e.g. an emoji outside
 * the BMP) is 2 UTF-16 units but 1 code point, so offsets drift without conversion.
 *
 * Used at every JS→Python position bridge for the pinned-region feature
 * and any future code-point-anchored surface.
 *
 * Note: only the UTF-16→code-point direction is needed — the region resolver reads
 * `createAbsolutePositionFromRelativePosition(...).index` (a UTF-16 offset) from
 * the editor and converts it to a code-point offset for the backend wire shape.
 * The reverse direction (cp→UTF-16) has no consumer (the editor works in UTF-16
 * natively); it was removed as dead code.
 */

/** Convert a UTF-16 code-unit offset to a code-point offset within `s`. */
export function utf16ToCp(s: string, utf16Offset: number): number {
  let cp = 0;
  let u16 = 0;
  while (u16 < utf16Offset && u16 < s.length) {
    // A surrogate pair = 2 UTF-16 units, 1 code point.
    const code = s.charCodeAt(u16);
    const step = code >= 0xd800 && code <= 0xdbff && u16 + 1 < s.length ? 2 : 1;
    u16 += step;
    cp += 1;
  }
  return cp;
}
