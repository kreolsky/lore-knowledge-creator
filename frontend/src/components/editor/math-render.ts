/**
 * KaTeX rendering with caching and math delimiter detection.
 *
 * ARCH: Separate module from live-preview-plugin to isolate KaTeX dependency
 * and keep the plugin focused on CM6 decoration logic.
 */
// SYSTEM: math-render — KaTeX rendering for CM6 (cached, inline + display + block math detection)

import katex from 'katex';
// ARCH: the KaTeX stylesheet moved
// here from index.css so it loads with the (lazy) editor chunk instead of every
// page incl. the unauthenticated landing page. math-render is the single KaTeX
// consumer, so co-locating the CSS keeps the JS + styles on one chunk boundary.
import 'katex/dist/katex.min.css';
import type { Text } from '@codemirror/state';
import { escapeHtml } from '../../utils/html';
import { createBoundedMemo } from '../../utils/bounded-memo';

// ─── Render cache ─────────────────────────────────────────────────────────────

// WHY bounded LRU (was: clear-at-threshold at 500): crossing the bound mid-edit
// used to wipe the whole map, forcing the next decoration rebuild to re-render
// the entire visible window in one frame. Measured 2026-09-09 (real katex, plan
// resource-cache-one-primitive step 6): worst post-warm rebuild batch 27 → 2.
// Policy and measurement live in utils/bounded-memo.ts.
const cache = createBoundedMemo<string>(500);

export function renderMathCached(latex: string, displayMode: boolean): string {
  const key = `${displayMode ? 'D' : 'I'}:${latex}`;
  const cached = cache.get(key);
  if (cached !== undefined) return cached;

  let html: string;
  try {
    html = katex.renderToString(latex, { displayMode, throwOnError: false });
  } catch {
    html = `<span class="cm-math-error">${escapeHtml(latex)}</span>`;
  }
  cache.set(key, html);
  return html;
}

// ─── Inline math detection ($...$) ───────────────────────────────────────────

// ARCH: Negative lookbehind/lookahead for $$ prevents matching block delimiters.
// Requires non-whitespace after opening $ and before closing $ to avoid $10-style false positives.
const INLINE_MATH_RE = /(?<!\$)\$(?!\$)(?!\s)(.+?)(?<!\s|\$)\$(?!\$)/g;

export interface MathRange {
  from: number;
  to: number;
  latex: string;
}

export function findInlineMath(lineText: string, lineFrom: number): MathRange[] {
  const results: MathRange[] = [];
  INLINE_MATH_RE.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = INLINE_MATH_RE.exec(lineText)) !== null) {
    results.push({
      from: lineFrom + m.index,
      to: lineFrom + m.index + m[0].length,
      latex: m[1],
    });
  }
  return results;
}

// ─── Single-line display math ($$...$$) ──────────────────────────────────────

const SINGLE_LINE_BLOCK_RE = /\$\$(.+?)\$\$/g;

export function findSingleLineDisplayMath(lineText: string, lineFrom: number): MathRange[] {
  const results: MathRange[] = [];
  SINGLE_LINE_BLOCK_RE.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = SINGLE_LINE_BLOCK_RE.exec(lineText)) !== null) {
    results.push({
      from: lineFrom + m.index,
      to: lineFrom + m.index + m[0].length,
      latex: m[1],
    });
  }
  return results;
}

// ─── Multi-line block math detection ($$\n...\n$$) ──────────────────────────

export function findBlockMath(doc: Text): MathRange[] {
  const results: MathRange[] = [];
  const text = doc.toString();
  const re = /^\$\$[ \t]*\n([\s\S]+?)\n[ \t]*\$\$$/gm;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    results.push({
      from: m.index,
      to: m.index + m[0].length,
      latex: m[1],
    });
  }
  return results;
}
