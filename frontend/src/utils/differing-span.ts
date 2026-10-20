/** One-change text diff for the public page's in-place refresh, and mapping a range through it. */

export interface TextChange {
  from: number;
  to: number;
  insert: string;
}

/** The one change turning `from` into `to` (common prefix/suffix kept); null when equal. */
export function differingSpan(from: string, to: string): TextChange | null {
  if (from === to) return null;
  const max = Math.min(from.length, to.length);
  let pre = 0;
  while (pre < max && from.charCodeAt(pre) === to.charCodeAt(pre)) pre++;
  let suf = 0;
  while (suf < max - pre && from.charCodeAt(from.length - 1 - suf) === to.charCodeAt(to.length - 1 - suf)) suf++;
  return { from: pre, to: from.length - suf, insert: to.slice(pre, to.length - suf) };
}

/**
 * Carry the range [start, end) across `change`. Text replaced by the change leaves the
 * range — the start moves past the replacement, the end stops before it — so an edit at
 * either edge does not grow the range (the same binding as a note anchor). Null when
 * nothing of the range is left.
 */
export function mapRangeThrough(
  change: TextChange, start: number, end: number,
): { start: number; end: number } | null {
  const delta = change.insert.length - (change.to - change.from);
  const mapStart = start < change.from ? start : start >= change.to ? start + delta : change.from + change.insert.length;
  const mapEnd = end <= change.from ? end : end > change.to ? end + delta : change.from;
  return mapEnd > mapStart ? { start: mapStart, end: mapEnd } : null;
}
