/** Text-fragment pointers on `doc:<id>#text=…` links: build from a selection, parse, resolve.
 *
 * Grammar (W3C text fragments): `text=[prefix-,]start[,end][,-suffix]`.
 * - A passage up to RANGE_MIN chars is written whole (`start` alone = the exact text).
 * - A longer one is written as a range — its first and last few words — so the link's
 *   length does not grow with the passage; the full text rides in the chat quote above it.
 * - Words are added to the range ends, then `prefix`/`suffix` context, only until the
 *   pointer resolves back to exactly the selected passage, so the common link stays short.
 * A fragment without `text=` is a heading slug (useAnchorScroll).
 *
 * Positions index the editor's MARKDOWN SOURCE, not the rendered text: a selection
 * spanning `**`/link syntax carries it into the pointer, which is also the source the
 * agent's edit_document addresses.
 */

const TEXT_PREFIX = 'text=';
const RANGE_MIN = 80;
const RANGE_WORDS = [3, 4, 5, 6, 8];
const CONTEXT_CAP = 32;

// WHY: only the characters that break the link are escaped, letters of every script stay
// raw — the pointer sits in the chat draft and the agent's prompt, and a fully
// percent-encoded Cyrillic passage is unreadable to both. Escaped: whitespace (ends a
// link destination), `%` (the escape itself), `( ) [ ] < >` (a raw `)` ends the target in
// every `[^)]+` link regex — editor patterns and backend/mentions.py), `#` `&` (URL
// syntax), `,` `-` (the `prefix-,` / `,-suffix` delimiters), `\` `` ` `` `"` `'` (markdown
// escapes/quoting). decodeURIComponent reads the mixed form, and the browser re-encodes
// the rest of a hash on navigation.
const LINK_UNSAFE = /[\s%()[\]<>#&,\-\\`"']/gu;

function encodePart(text: string): string {
  return text.replace(LINK_UNSAFE, c => encodeURIComponent(c).replace(
    /[()\-'*]/g, x => `%${x.charCodeAt(0).toString(16).toUpperCase()}`,
  ));
}

export interface TextFragment {
  prefix?: string;
  /** The exact passage, or the range's first words when `end` is set. */
  start: string;
  /** The range's last words; absent for an exact passage. */
  end?: string;
  suffix?: string;
}

/** Context before `from`, at most CONTEXT_CAP chars, starting on a word boundary. */
function prefixAt(source: string, from: number): string {
  const begin = Math.max(0, from - CONTEXT_CAP);
  const slice = source.slice(begin, from);
  return begin > 0 ? slice.replace(/^\S*\s/, '') : slice;
}

/** Context after `to`, at most CONTEXT_CAP chars, ending on a word boundary. */
function suffixAt(source: string, to: number): string {
  const stop = Math.min(source.length, to + CONTEXT_CAP);
  const slice = source.slice(to, stop);
  return stop < source.length ? slice.replace(/\s\S*$/, '') : slice;
}

/** First and last `n` words of `text` (raw slices), or null when they would meet. */
function rangeEnds(text: string, n: number): { start: string; end: string } | null {
  const words = [...text.matchAll(/\S+/g)];
  if (words.length < 2 * n + 1) return null;
  const head = words[n - 1];
  return { start: text.slice(0, head.index + head[0].length), end: text.slice(words[words.length - n].index) };
}

function serialize(frag: TextFragment): string {
  const parts = [encodePart(frag.start)];
  if (frag.end) parts.push(encodePart(frag.end));
  if (frag.prefix) parts.unshift(`${encodePart(frag.prefix)}-`);
  if (frag.suffix) parts.push(`-${encodePart(frag.suffix)}`);
  return TEXT_PREFIX + parts.join(',');
}

/**
 * The `text=…` fragment (without `#`) naming source[from, to), or null for a
 * whitespace-only selection. Surrounding whitespace is trimmed off the passage.
 */
export function buildTextFragment(source: string, from: number, to: number): string | null {
  const raw = source.slice(from, to);
  const begin = from + (raw.length - raw.trimStart().length);
  const text = raw.trim();
  if (!text) return null;
  const finish = begin + text.length;
  const context = { prefix: prefixAt(source, begin) || undefined, suffix: suffixAt(source, finish) || undefined };
  const hits = (frag: TextFragment) => {
    const r = resolveTextFragment(source, frag);
    return r !== null && r.from === begin && r.to === finish;
  };
  const candidates: TextFragment[] = [];
  if (text.length > RANGE_MIN) {
    const ranges = RANGE_WORDS.map(n => rangeEnds(text, n)).filter((r): r is TextFragment & { end: string } => r !== null);
    candidates.push(...ranges);
    if (ranges[0]) candidates.push({ ...ranges[0], ...context });
  }
  candidates.push({ start: text }, { start: text, ...context });
  return serialize(candidates.find(hits) ?? candidates[candidates.length - 1]);
}

/** Parse a raw (still percent-encoded) fragment; null when it is not a `text=` pointer. */
export function parseTextFragment(fragment: string): TextFragment | null {
  if (!fragment.startsWith(TEXT_PREFIX)) return null;
  const parts = fragment.slice(TEXT_PREFIX.length).split(',');
  const prefix = parts[0]?.endsWith('-') ? parts.shift()!.slice(0, -1) : undefined;
  const suffix = parts.length > 1 && parts[parts.length - 1].startsWith('-') ? parts.pop()!.slice(1) : undefined;
  if (parts.length < 1 || parts.length > 2 || parts.some(p => !p)) return null;
  try {
    const decode = (p: string | undefined) => (p !== undefined ? decodeURIComponent(p) : undefined);
    return { start: decodeURIComponent(parts[0]), end: decode(parts[1]), prefix: decode(prefix), suffix: decode(suffix) };
  } catch {
    return null; // malformed percent-encoding: the caller reports it as not found
  }
}

/** True when `fragment` is a `text=` pointer (vs a heading slug). */
export function isTextFragment(fragment: string): boolean {
  return fragment.startsWith(TEXT_PREFIX);
}

/**
 * Locate the passage in `source`: each occurrence of `start` (extended to the nearest
 * following `end` for a range) is a candidate; the best prefix/suffix context match
 * wins, then the shortest span, then the first. Null when nothing matches.
 */
export function resolveTextFragment(source: string, frag: TextFragment): { from: number; to: number } | null {
  let best: { from: number; to: number; score: number } | null = null;
  for (let i = source.indexOf(frag.start); i !== -1; i = source.indexOf(frag.start, i + 1)) {
    let to = i + frag.start.length;
    if (frag.end) {
      const j = source.indexOf(frag.end, to);
      if (j === -1) break; // no later occurrence of `start` can reach an `end` either
      to = j + frag.end.length;
    }
    const score = (frag.prefix && source.slice(0, i).endsWith(frag.prefix) ? 1 : 0)
      + (frag.suffix && source.startsWith(frag.suffix, to) ? 1 : 0);
    if (!best || score > best.score || (score === best.score && to - i < best.to - best.from)) {
      best = { from: i, to, score };
    }
  }
  return best ? { from: best.from, to: best.to } : null;
}
