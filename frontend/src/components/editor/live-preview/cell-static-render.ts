/**
 * Lazy table-cell placeholder rendered by the live-preview pipeline itself.
 *
 * WHY one shared offscreen EditorView: a placeholder must render markdown exactly like the
 * cell's editor (links collapsed, wiki-links, bare URLs, ligatures) or the cell visibly
 * changes on its first click — a second, hand-written markdown renderer drifted from the
 * editor that way. One read-only view built from `staticCellExtensions()` renders each cell
 * text synchronously on dispatch; the placeholder takes a copy of its LINE CONTENT only.
 * WHY not the whole `.cm-editor` clone: thousands of cloned editor wrappers (editor →
 * scroller → content → line) made the browser's style recalc dominate opening a large table;
 * the inline marks are already styled by the host document editor's theme, and the cell box
 * (padding, wrapping) comes from `.cm-table-block-cell-lazy` in index.css.
 */
import { EditorView } from '@codemirror/view';
import { EditorState } from '@codemirror/state';
import { staticCellExtensions } from '../render-bundle';
import { LIGATURE_RULES } from '../ligature-plugin';

let renderer: EditorView | null = null;

function getRenderer(): EditorView {
  if (renderer) return renderer;
  const host = document.createElement('div');
  host.setAttribute('aria-hidden', 'true');
  host.style.cssText = 'position:fixed;left:-10000px;top:0;width:600px;visibility:hidden;pointer-events:none';
  document.body.appendChild(host);
  renderer = new EditorView({ parent: host, state: EditorState.create({ extensions: staticCellExtensions() }) });
  return renderer;
}

// Characters that can never start markdown or a link: letters, digits, spaces and a few
// neutral punctuation marks.
const NEUTRAL_RE = /^[\p{L}\p{N} .,;?%"'«»№]*$/u;
// Neutral characters that still parse as markdown: an ordered-list marker (`1.`) and a
// leading indent (4 spaces = code block).
const NEUTRAL_BUT_MARKDOWN_RE = /^(\d+\.(\s|$)|\s)/;

/**
 * True when `text` renders as itself: no character in it can start markdown or a link and it
 * holds no ligature pattern (checked against the live rule list), so the full render would
 * only wrap it in the cell skin.
 */
export function isPlainCellText(text: string): boolean {
  return text !== ''
    && NEUTRAL_RE.test(text)
    && !NEUTRAL_BUT_MARKDOWN_RE.test(text)
    && !LIGATURE_RULES.some((r) => text.includes(r.pattern));
}

/**
 * Render cell markdown through the cell's live-preview pipeline: one plain `<div>` per line
 * holding a copy of the line's rendered content. Line-level decoration (classes other than
 * `cm-line`, inline style such as list indent) is carried over; `cm-line` itself is not, so
 * the document's line padding and `:has()` heading rules never match placeholder lines.
 */
export function renderCellThroughEditor(text: string): DocumentFragment {
  const view = getRenderer();
  view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: text } });
  const frag = document.createDocumentFragment();
  for (const line of view.contentDOM.children) {
    const div = document.createElement('div');
    const classes = [...line.classList].filter((c) => c !== 'cm-line');
    if (classes.length) div.className = classes.join(' ');
    const style = line.getAttribute('style');
    if (style) div.setAttribute('style', style);
    for (const node of line.childNodes) div.appendChild(node.cloneNode(true));
    frag.appendChild(div);
  }
  return frag;
}

/**
 * Placeholder content for a cell. WHY a plain-text fast path: a full render costs a CM6
 * dispatch per cell, which makes a large imported CSV (mostly numbers and plain words) slow
 * to open; plain text renders as itself, so it is a bare text node.
 */
export function renderCellStatic(text: string): Node {
  return isPlainCellText(text) ? document.createTextNode(text) : renderCellThroughEditor(text);
}
