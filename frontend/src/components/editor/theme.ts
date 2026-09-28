/**
 * Centralized CM6 theme extensions for all editor instances.
 *
 * WHY: loreEditorTheme targets the main document editor (CodeMirrorEditor).
 *
 * Replaces global CSS !important overrides with scoped EditorView.theme()
 * calls. Each CM6 instance gets a unique scope class (e.g. .ͼXX), so
 * themes don't cross-contaminate between editor instances.
 *
 * Widget CSS (.cm-task-checkbox, .cm-table-widget, .cm-code-copy-btn)
 * stays in index.css — widgets are instance-independent DOM elements.
 */

import { type Extension } from '@codemirror/state';
import { EditorView } from '@codemirror/view';

const MONO = "'JetBrains Mono', ui-monospace, SFMono-Regular, monospace";
const CODE_BG = 'var(--surface2)';
const CODE_FONT_SIZE = '0.875em';

// Find-match highlight tokens (searchHighlight plugin — find-matches.ts). WHY amber,
// distinct from the green .cm-selectionMatch (occurrences of the selected WORD): the
// Find tab's query match set is case/regexp/whole-word aware, so it gets its own token;
// the match under the primary selection (the one findNext landed on) gets the accent.
const SEARCH_MATCH_BG = 'rgba(217, 119, 6, 0.30)';
const SEARCH_MATCH_SELECTED_BG = 'rgba(98, 85, 224, 0.45)';

// ── Main Document Editor ────────────────────────────────────────

export const loreEditorTheme: Extension = [
  EditorView.theme({
  '&': { height: 'auto', fontFamily: 'inherit', background: 'transparent' },

  '.cm-scroller': {
    fontFamily: 'inherit',
    padding: '2rem',
    background: 'var(--bg)',
    scrollbarWidth: 'thin',
    scrollbarColor: 'var(--border) transparent',
  },

  '.cm-content': {
    fontFamily: 'inherit',
    fontSize: '1rem',
    lineHeight: '1.6',
    color: 'var(--text)',
    caretColor: 'transparent',
  },

  // Gutters, cursor, selection
  '.cm-gutters': { background: 'transparent', borderRight: 'none' },
  '.cm-activeLine': { backgroundColor: 'transparent' },

  // WHY: native browser selection (no drawSelection()) — renders ON TOP of text,
  // visible regardless of element backgrounds (code blocks, inline code).
  '& .cm-content ::selection': {
    backgroundColor: 'var(--accent-glow) !important',
  },
  'html.dark & .cm-content ::selection': {
    backgroundColor: 'var(--accent-glow) !important',
  },

  // ── Headers ──

  '.cm-header-1': { fontSize: '1.875rem', fontWeight: '700', lineHeight: '1.25', color: 'var(--text)', textDecoration: 'none' },
  '.cm-header-2': { fontSize: '1.5rem', fontWeight: '700', lineHeight: '1.25', color: 'var(--text)', textDecoration: 'none' },
  '.cm-header-3': { fontSize: '1.25rem', fontWeight: '600', lineHeight: '1.3', color: 'var(--text)', textDecoration: 'none' },
  '.cm-header-4': { fontSize: '1.1rem', fontWeight: '600', lineHeight: '1.3', color: 'var(--text)', textDecoration: 'none' },
  '.cm-header-5': { fontSize: '1rem', fontWeight: '600', color: 'var(--text)', textDecoration: 'none' },
  '.cm-header-6': { fontSize: '1rem', fontWeight: '500', color: 'var(--text-muted)', textDecoration: 'none' },

  // Heading top spacing
  '.cm-line:has(.cm-header-1)': { paddingTop: '1.35rem' },
  '.cm-line:has(.cm-header-2)': { paddingTop: '1.05rem' },
  '.cm-line:has(.cm-header-3)': { paddingTop: '0.825rem' },
  '.cm-line:has(.cm-header-4)': { paddingTop: '0.64rem' },
  '.cm-line:has(.cm-header-5)': { paddingTop: '0.49rem' },
  '.cm-line:has(.cm-header-6)': { paddingTop: '0.49rem' },

  // FRAGILE: child combinator + :has() — depends on .cm-line being direct child of .cm-content.
  // Skip first line — no spacing above the document title.
  '.cm-content > .cm-line:first-child:has(.cm-header-1)': { paddingTop: '0' },
  '.cm-content > .cm-line:first-child:has(.cm-header-2)': { paddingTop: '0' },
  '.cm-content > .cm-line:first-child:has(.cm-header-3)': { paddingTop: '0' },

  // ── Text formatting ──

  '.cm-strong': { fontWeight: '600' },
  '.cm-em': { fontStyle: 'italic' },
  '.cm-strikethrough': { textDecoration: 'line-through', opacity: '0.45' },

  '.cm-inline-code': {
    backgroundColor: CODE_BG,
    padding: '0.1rem 0',
    borderRadius: '0',
    fontFamily: MONO,
    fontSize: CODE_FONT_SIZE,
  },

  '.cm-color-swatch': {
    padding: '0.1rem 0.3rem',
  },
  '.cm-color-swatch .cm-inline-code': {
    backgroundColor: 'transparent',
    padding: '0',
    fontFamily: 'inherit',
    fontSize: 'inherit',
  },
  '.cm-inline-code.cm-color-swatch': {
    fontFamily: 'inherit',
    fontSize: 'inherit',
  },

  // Search + bracket highlights interacting with inline code.
  // WHY: CM6 renders Decoration.mark layers as nested spans; nesting order between
  // syntaxHighlighting('cm-inline-code') and search/bracketMatching depends on extension
  // precedence and is not stable across CM6 versions. We handle ALL three cases:
  //
  //   1. Equal range  →  one span gets both classes  →  .a.b sets the highlight bg.
  //   2. inline-code OUTER, highlight INNER  →  inner paints over outer's gray bg (works by default).
  //      We still set the bg explicitly to use the same green tint as the standalone case.
  //   3. highlight OUTER, inline-code INNER  →  inner's gray bg covers outer's highlight.
  //      Fix: zero out inline-code's background when nested under a highlight wrapper.
  //
  // CM6 baseTheme defines .cm-selectionMatch at 0,1,0; .cm-content prefix gives us 0,3,0.
  '.cm-content .cm-inline-code.cm-selectionMatch': { backgroundColor: '#99ff7780' },
  '.cm-content .cm-inline-code.cm-matchingBracket': { backgroundColor: '#328c8252' },
  '.cm-content .cm-inline-code.cm-nonmatchingBracket': { backgroundColor: '#bb555544' },
  '.cm-content .cm-inline-code .cm-selectionMatch': { backgroundColor: '#99ff7780' },
  '.cm-content .cm-inline-code .cm-matchingBracket': { backgroundColor: '#328c8252' },
  '.cm-content .cm-inline-code .cm-nonmatchingBracket': { backgroundColor: '#bb555544' },
  '.cm-content .cm-selectionMatch .cm-inline-code': { backgroundColor: 'transparent' },
  '.cm-content .cm-matchingBracket .cm-inline-code': { backgroundColor: 'transparent' },
  '.cm-content .cm-nonmatchingBracket .cm-inline-code': { backgroundColor: 'transparent' },

  // ── Find-match highlighting (searchHighlight plugin) ──
  // Same nesting matrix as selectionMatch above so a match spanning `code` stays
  // visible regardless of which mark layer nests inside which (precedence is not
  // stable across CM6 versions — see the selectionMatch comment). -selected must be
  // declared AFTER the base rule to win at equal specificity.
  '.cm-searchMatch': { backgroundColor: SEARCH_MATCH_BG, borderRadius: '0' },
  '.cm-searchMatch-selected': { backgroundColor: SEARCH_MATCH_SELECTED_BG },
  '.cm-content .cm-inline-code.cm-searchMatch': { backgroundColor: SEARCH_MATCH_BG },
  '.cm-content .cm-inline-code .cm-searchMatch': { backgroundColor: SEARCH_MATCH_BG },
  '.cm-content .cm-searchMatch .cm-inline-code': { backgroundColor: 'transparent' },

  // ── Fenced code blocks ──
  // WHY: Code block styling is split into two layers:
  //   1. Base span reset — forces monospace font, normal line height, zero padding
  //      on ALL <span> inside code blocks. Does NOT reset color/fontWeight/fontStyle
  //      so language-specific syntax highlighting (YAML, JSON, etc.) can pass through.
  //   2. Targeted neutralization — resets color/size/weight only for markdown-specific
  //      classes (cm-header-*, cm-strong, cm-em, etc.) that could leak into code blocks.
  // WHY background is NOT reset: would override CM6's .cm-selectionMatch /
  // .cm-matchingBracket backgrounds (they have lower specificity, 0,1,0) and kill all
  // search/bracket highlighting inside code blocks.

  '.cm-fenced-code': {
    background: CODE_BG,
    fontFamily: MONO,
    fontSize: CODE_FONT_SIZE,
    paddingLeft: '1rem',
    paddingRight: '1rem',
  },
  '.cm-fenced-code.cm-fenced-code-first': { paddingTop: '0.5rem', borderTop: 'none' },
  '.cm-fenced-code.cm-fenced-code-last': { paddingBottom: '0.5rem', borderBottom: 'none' },

  // Fence lines (``` markers) — invisible text, standard line height.
  // WHY not height:6px: CM6 virtual viewport estimates off-screen line heights
  // at defaultLineHeight. Collapsing fence lines causes scroll jumps.
  '.cm-fenced-code-fence': { color: 'transparent' },
  '.cm-fenced-code-fence-first': { paddingTop: '0.5rem', position: 'relative' },
  '.cm-fenced-code-fence-last': { paddingBottom: '0.5rem' },
  '.cm-fenced-code-fence--editing': { color: 'inherit' },

  // Base span reset — monospace + layout only, preserves color for syntax highlighting
  '.cm-fenced-code span': {
    fontFamily: MONO,
    fontSize: 'inherit',
    lineHeight: 'inherit',
    padding: '0',
  },

  // Scoped syntax highlighting colors for code blocks
  // WHY: code-highlight.ts uses `class` (not `color`) to avoid leaking colors
  // into regular markdown text. Actual colors are scoped here to .cm-fenced-code.
  '.cm-fenced-code .tok-code-key': { color: 'var(--code-key)' },
  '.cm-fenced-code .tok-code-string': { color: 'var(--code-string)' },
  '.cm-fenced-code .tok-code-plain-scalar': { color: 'var(--code-plain-scalar)' },
  '.cm-fenced-code .tok-code-number': { color: 'var(--code-number)' },
  '.cm-fenced-code .tok-code-keyword': { color: 'var(--code-keyword)' },
  '.cm-fenced-code .tok-code-comment': { color: 'var(--code-comment)' },
  '.cm-fenced-code .tok-code-type': { color: 'var(--code-type)' },

  // Targeted neutralization of markdown formatting inside code blocks
  '.cm-fenced-code .cm-header-1, .cm-fenced-code .cm-header-2, .cm-fenced-code .cm-header-3, .cm-fenced-code .cm-header-4, .cm-fenced-code .cm-header-5, .cm-fenced-code .cm-header-6': {
    color: 'inherit',
    fontSize: 'inherit',
    fontWeight: 'inherit',
    textDecoration: 'inherit',
  },
  '.cm-fenced-code .cm-strong': { fontWeight: 'inherit' },
  '.cm-fenced-code .cm-em': { fontStyle: 'inherit' },
  '.cm-fenced-code .cm-strikethrough': { textDecoration: 'inherit', opacity: '1' },
  '.cm-fenced-code .cm-inline-code': { background: 'none', padding: '0' },

  // ── Lists fallback for list-indent.ts ──

  '.cm-list-line': {
    paddingLeft: '1.2em',
    textIndent: '-1.2em',
  },

// ── Blockquotes ──

'.cm-quote': {
  borderLeft: '4px solid var(--border)',
  paddingLeft: '1rem',
  color: 'var(--text-muted)',
},

  // ── Links ──

  '.cm-link': {
    color: 'var(--text)',
    textDecoration: 'none !important',
    borderBottom: '2px dashed var(--text)',
  },
  '.cm-link:hover': { borderBottomColor: 'transparent' },

  // ── HR line ──

  '.cm-hr-line': { position: 'relative' },
  '.cm-hr-line::after': {
    content: '""',
    position: 'absolute',
    top: '50%',
    left: '0',
    right: '0',
    borderTop: '1px solid var(--border)',
  },

  // Task done (strikethrough on checked items)
  '.cm-task-done': { textDecoration: 'line-through', opacity: '0.45' },

  // ── Transclusion (block embed of another document/reference content) ──
  // see SYSTEM: transclusion
  // WHY: standalone lines render as a CM6 block widget (build-structural.ts), so the
  // band lives BETWEEN editor lines — no line-height strut, gapless top/bottom.
  // WHY: full-bleed background. The band breaks out of the centered max-width .cm-content
  // (844px) to the .cm-scroller border so the gray reaches the full editor column width
  // (like the top banner). `.cm-scroller` is a CSS container (index.css): 100cqw = its
  // content box, 4rem = its 2rem padding ×2, 100% = the .cm-content width. The negative
  // margins extend the band to the scroller border-box; the equal padding pulls the band's
  // CONTENT box back to exactly 844px (the host text column). Header + body are then an
  // inner `max-width: 844px; margin: 0 auto` column inside that 844 content box, so the
  // title and embedded text line up with the host document text.
  // WHY: overflow: hidden stays on the BODY (the 844 inner column), so any wide
  // child (table/long URL) clips at the text column, never reaching the scroller.  Why: clipping wide children at the text column keeps them inside the transclusion band; without overflow:hidden on the body they'd reach the page scroller and break layout.
  '.cm-transclusion': {
    background: 'var(--surface-transclusion)',
    border: 'none',
    borderRadius: '0',
    margin: '0',
    marginLeft: 'calc((100% - 100cqw - 4rem) / 2)',
    marginRight: 'calc((100% - 100cqw - 4rem) / 2)',
    paddingLeft: 'calc((100cqw + 4rem - 100%) / 2)',
    paddingRight: 'calc((100cqw + 4rem - 100%) / 2)',
    maxWidth: 'none',
  },
  // WHY: header is an inner 844px centered column (same as the body) carrying the
  // divider; the title inside is inline-block so its hover/click target is the title
  // TEXT only. The 844 centering is what aligns the title with the host text column.
  '.cm-transclusion-header': {
    maxWidth: '844px',
    marginLeft: 'auto',
    marginRight: 'auto',
    borderBottom: '1px solid var(--border)',
  },
  '.cm-transclusion-title': {
    display: 'inline-block',
    maxWidth: '100%',
    cursor: 'pointer',
    fontWeight: 'normal',
    fontSize: '1rem',
    lineHeight: '1.4',
    // WHY: 6px left padding matches CM6's default `.cm-line` padding-left.
    // Why: the title is a plain div (no line padding) but the embedded/host text lives
    // in `.cm-line`s inset 6px; without this the title GLYPHS sit 6px left of the text
    // even though the boxes align. Measured: title glyph 273 vs text glyph 279.
    padding: '0 0 0 6px',
    color: 'var(--text)',
    whiteSpace: 'nowrap',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    userSelect: 'none',
  },
  '.cm-transclusion-title:hover': { textDecoration: 'underline' },
  '.cm-transclusion-body': {
    maxWidth: '844px',
    marginLeft: 'auto',
    marginRight: 'auto',
    fontSize: '1rem',
    lineHeight: '1.5',
    padding: '0',
    color: 'var(--text)',
    textIndent: '0',
    overflow: 'hidden',
    overflowWrap: 'break-word',
  },
  '.cm-transclusion-loading .cm-transclusion-spinner': {
    minHeight: '40px',
    background: 'var(--surface2)',
  },

  // Typographic ligatures (ligature-plugin.ts)
  '.cm-ligature': { fontFamily: 'inherit' },

  // ── Inline document links ──

  // ── Shared link base (dashed underline, pointer, no underline on hover) ──
  // WHY: all link types share identical text style; only backgroundColor differs.  Why: link types differ only by background highlight (note/ref/doc/ext); sharing the text style keeps inline links visually consistent.
  '.cm-note-link, .cm-ref-link, .cm-doc-link, .cm-ext-link': {
    color: 'var(--text)',
    cursor: 'pointer',
    padding: '0 2px',
    borderRadius: '0',
    textDecoration: 'none',
    borderBottom: '2px dashed var(--text)',
  },
  // Kill .cm-link border inside inline links (nested spans → double underline otherwise)
  '.cm-note-link .cm-link, .cm-ref-link .cm-link, .cm-doc-link .cm-link, .cm-ext-link .cm-link': {
    borderBottom: 'none',
  },
  '.cm-note-link:hover, .cm-ref-link:hover, .cm-doc-link:hover, .cm-ext-link:hover': {
    borderBottomColor: 'transparent',
  },

  // Per-type backgrounds
  '.cm-note-link': { backgroundColor: 'var(--sticky-yellow-light)', borderBottom: '2px dashed var(--sticky-yellow-dark)' },
  '.cm-note-link:hover': { backgroundColor: 'var(--sticky-yellow-dark)' },
  'html.dark & .cm-note-link': { backgroundColor: 'var(--sticky-yellow-light)', borderBottom: '2px dashed var(--sticky-yellow-dark)' },
  'html.dark & .cm-note-link:hover': { backgroundColor: 'var(--sticky-yellow-dark)' },

  '.cm-ref-link': { backgroundColor: 'var(--sticky-blue-light)', borderBottom: '2px dashed var(--sticky-blue-dark)' },
  '.cm-ref-link:hover': { backgroundColor: 'var(--sticky-blue-dark)' },
  'html.dark & .cm-ref-link': { backgroundColor: 'var(--sticky-blue-light)', borderBottom: '2px dashed var(--sticky-blue-dark)' },
  'html.dark & .cm-ref-link:hover': { backgroundColor: 'var(--sticky-blue-dark)' },

  '.cm-doc-link': { backgroundColor: 'var(--surface3)' },
  '.cm-doc-link:hover': { backgroundColor: 'var(--border)' },

  // Broken link styles — applied when link target ID is not found in store
  '.cm-doc-link-broken, .cm-note-link-broken, .cm-ref-link-broken': {
    color: 'var(--text-muted)',
    cursor: 'help',
    padding: '0 2px',
    borderRadius: '0',
    textDecoration: 'line-through',
    borderBottom: '2px dashed var(--text-muted)',
  },
  '.cm-doc-link-broken:hover, .cm-note-link-broken:hover, .cm-ref-link-broken:hover': {
    borderBottomColor: 'transparent',
  },
}),
];
