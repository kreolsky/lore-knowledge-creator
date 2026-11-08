/**
 * CM6 extension: Obsidian-style paste with HTML→Markdown conversion.
 *
 * Cmd+V      → paste with formatting when the clipboard HTML carries it; otherwise the
 *              verbatim `text/plain` source (Notepad/terminal/chat/code-editor wrappers that
 *              put formatting-free `text/html` on the clipboard alongside `text/plain`).
 * Cmd+Shift+V → paste plain text (browser default, strips everything)
 *
 * ARCH: Uses EditorView.domEventHandlers to intercept paste before CM6's default handler.
 * Three sources resolve in order: Lore-origin (marker) → unformatted external (verbatim
 * text/plain) → formatted external (HTML→Markdown via Turndown). An unformatted GFM table
 * is still upgraded to a live table object on the verbatim path, never downgraded to inert
 * pipe text by the browser default.
 * SYSTEM: editor — clipboard paste handler
 */

import { EditorView } from '@codemirror/view';
import { syntaxTree } from '@codemirror/language';
import type { SyntaxNode } from '@lezer/common';
import TurndownService from 'turndown';
import { gfm } from 'turndown-plugin-gfm';
import { entityYdoc } from './active-editor';
import { importGfmTables } from '../components/editor/live-preview/gfm-table-import';
import { t } from '../i18n';

const turndown = new TurndownService({
  headingStyle: 'atx',
  codeBlockStyle: 'fenced',
  bulletListMarker: '-',
  emDelimiter: '*',
  strongDelimiter: '**',
});
turndown.use(gfm);

// Private sentinel written by the Lore copy path. Its presence in `text/html` tells the
// paste handler to use the lossless `text/plain` markdown source instead of Turndown.
const LORE_CLIP_MARK = '<!--lore-clipboard-->';

// An intraword `_` is never an emphasis delimiter (CommonMark/GFM), so Turndown escaping
// it is unconditionally wrong. We shield intraword underscores before delegating to Turndown's
// own escape, then restore — the rest of Turndown's escape table stays derived from Turndown,
// so this fix cannot drift from its list. The placeholder is a private-use code point that
// cannot occur in authored markdown.
const INTRAWORD_UNDERSCORE = /(?<=[\p{L}\p{N}])_(?=[\p{L}\p{N}])/gu;
const UNDERSCORE_PLACEHOLDER = '\uE000';

// Shield literal [ and ] so Turndown's global \[ / \] escape rules don't fire. The editor
// renders links natively (real <a> → [text](url) via Turndown's link RULE, not escape), so
// escaping literal brackets is pure noise that corrupts [[wikilinks]], [note], a[0], [1].
// Shield+restore (not post-strip) because the rules are global and interleave with
// backslash-doubling (input "\[" → "\\\["), making the inserted backslash ambiguous to strip.
const BRACKET_OPEN_PLACEHOLDER = '\uE001';
const BRACKET_CLOSE_PLACEHOLDER = '\uE002';

// Reverse Turndown's line-start "1. " → "1\. " escape: the editor renders ordered lists
// natively, so the backslash is noise. Post-strip is safe here — the rule is ^-anchored and
// never double-escapes, so the inserted backslash is unambiguous (a user's "\1. x" → "\\1. x"
// does not match and is preserved).
const LEADING_ORDERED_LIST = /^(\d+)\\\./;

// Reverse Turndown's leading `-` / `=` escapes where no block construct can follow: a list item
// or thematic break needs `-` + space/dash, a setext underline is a line of `=` only. "-160000"
// or "=SUM(A1)" from a spreadsheet are values, and the backslash landed in the cell verbatim.
// Post-strip is unambiguous for the same reason as above: a user's own "\-5" is doubled first.
const LEADING_SAFE_DASH = /^\\-(?=[^\s-])/;
const LEADING_SAFE_EQUALS = /^\\(=+)(?=[^=\s])/;

turndown.escape = (string: string): string => {
  const shielded = string
    .replace(INTRAWORD_UNDERSCORE, UNDERSCORE_PLACEHOLDER)
    .replace(/\[/g, BRACKET_OPEN_PLACEHOLDER)
    .replace(/\]/g, BRACKET_CLOSE_PLACEHOLDER);
  const escaped = TurndownService.prototype.escape.call(turndown, shielded) as string;
  return escaped
    .split(UNDERSCORE_PLACEHOLDER)
    .join('_')
    .split(BRACKET_OPEN_PLACEHOLDER)
    .join('[')
    .split(BRACKET_CLOSE_PLACEHOLDER)
    .join(']')
    .replace(LEADING_ORDERED_LIST, '$1.')
    .replace(LEADING_SAFE_DASH, '-')
    .replace(LEADING_SAFE_EQUALS, '$1');
};

// Tags whose presence means the clipboard HTML carries real semantic formatting → Turndown.
const FORMATTING_SELECTOR =
  'strong,b,em,i,a,h1,h2,h3,h4,h5,h6,ul,ol,li,table,code,pre,blockquote,img,del,s';

turndown.addRule('strikethrough', {
  filter: ['del', 's'],
  replacement: (content) => `~~${content}~~`,
});

turndown.addRule('removeStyles', {
  filter: ['style', 'script', 'meta'],
  replacement: () => '',
});

turndown.addRule('tableCellFlatten', {
  filter: ['td', 'th'],
  replacement(content, node) {
    const flat = content
      .replace(/\n{2,}/g, ' ')
      .replace(/\n/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();
    // Mirror turndown-plugin-gfm's cell(): the FIRST cell of a row carries the leading `|`
    // so the assembled row is `| a | b |`. Our importer (gfm-table-import.isPipeLine) requires
    // a leading pipe, so without this the first cell's leading space would make every pasted
    // table row miss recognition. (cellIndex is standard on td/th in DOM + jsdom.)
    const prefix = (node as HTMLTableCellElement).cellIndex === 0 ? '| ' : ' ';
    return `${prefix}${flat} |`;
  },
});

/**
 * Normalize clipboard tables before Turndown: unfold merged cells, flatten each cell to one line (`<br>`/`<p>` →
 * space, attributes dropped) and promote the first row of a header-less table to `<th>`.
 * WHY the promotion: turndown-plugin-gfm converts only a table whose first row is a heading
 * row and `keep`s every other one as raw HTML; Google Docs/Sheets and spreadsheets copy
 * all-`<td>` tables, which landed in the document as inert HTML. GFM requires a header, so
 * the first row becomes it, moved into a `<thead>`.
 */
/**
 * Unfold colspan/rowspan into a plain grid: the text stays in the top-left cell of the merged
 * area, every other slot gets an empty cell of the same tag.
 * WHY: the table model is a rectangular grid with no spans; a dropped `rowspan` shifted every
 * later cell of the rows below it under the wrong column — silent data corruption on paste.
 */
function unfoldSpans(doc: Document, table: HTMLTableElement): void {
  const pending: number[] = []; // per column: rows still covered by a rowspan from above
  for (const row of [...table.rows]) {
    const cells = [...row.cells];
    const out: HTMLTableCellElement[] = [];
    const filler = (tag: string) => doc.createElement(tag) as HTMLTableCellElement;
    let col = 0;
    const fillCovered = (tag: string) => {
      while ((pending[col] ?? 0) > 0) { pending[col] -= 1; out.push(filler(tag)); col += 1; }
    };
    for (const cell of cells) {
      const tag = cell.tagName.toLowerCase();
      fillCovered(tag);
      const colspan = Math.max(cell.colSpan || 1, 1);
      const rowspan = Math.max(cell.rowSpan || 1, 1);
      cell.removeAttribute('colspan');
      cell.removeAttribute('rowspan');
      for (let k = 0; k < colspan; k += 1) {
        out.push(k === 0 ? cell : filler(tag));
        pending[col] = rowspan - 1;
        col += 1;
      }
    }
    fillCovered(cells[cells.length - 1]?.tagName.toLowerCase() ?? 'td');
    row.replaceChildren(...out);
  }
}

function preprocessTableHtml(html: string): string {
  if (!/<t[dh][\s>]/i.test(html)) return html;
  const doc = new DOMParser().parseFromString(html, 'text/html');
  doc.querySelectorAll('table').forEach((t) => unfoldSpans(doc, t as HTMLTableElement));
  for (const cell of doc.querySelectorAll('td, th')) {
    cell.querySelectorAll('br').forEach((br) => br.replaceWith(' '));
    cell.querySelectorAll('p').forEach((p) => p.replaceWith(' ', ...p.childNodes, ' '));
    const flat = cell.innerHTML.replace(/\s+/g, ' ').trim();
    const clean = doc.createElement(cell.tagName.toLowerCase());
    clean.innerHTML = flat;
    cell.replaceWith(clean);
  }
  for (const table of doc.querySelectorAll('table')) {
    const first = (table as HTMLTableElement).rows[0];
    if (!first || first.querySelector('th')) continue;
    for (const td of [...first.cells]) {
      const th = doc.createElement('th');
      th.append(...td.childNodes);
      td.replaceWith(th);
    }
    // WHY a <thead>, not just <th>: the plugin accepts an all-<th> row only as the first child
    // of the FIRST tbody, and a `<colgroup>` (Sheets/Docs always emit one) disqualifies it.
    const thead = doc.createElement('thead');
    thead.append(first);
    table.prepend(thead);
  }
  return doc.body.innerHTML;
}

/**
 * Convert clipboard HTML to markdown via the paste-path Turndown config (preprocess tables,
 * then turndown). Exported so the copy→paste round-trip test exercises the SAME conversion
 * the live paste handler uses — no duplicated Turndown setup.
 */
export function htmlToMarkdown(html: string): string {
  return turndown.turndown(preprocessTableHtml(html));
}

/**
 * True when an inline style encodes bold/italic/underline emphasis. Word/Gmail encode bold as
 * `font-weight:700` rather than a `<strong>` tag, so a tag-only formatting test would silently
 * drop their formatting.
 */
function isStyleEmphasis(style: CSSStyleDeclaration): boolean {
  const weight = style.fontWeight;
  if (weight === 'bold') return true;
  const numeric = parseInt(weight, 10);
  if (!Number.isNaN(numeric) && numeric >= 600) return true;
  if (style.fontStyle === 'italic' || style.fontStyle === 'oblique') return true;
  if (/\b(?:underline|line-through)\b/.test(style.textDecoration)) return true;
  return false;
}

function hasStyleEmphasis(root: ParentNode): boolean {
  for (const el of root.querySelectorAll<HTMLElement>('[style]')) {
    if (isStyleEmphasis(el.style)) return true;
  }
  return false;
}

/**
 * True for a code-editor source copy (VSCode et al.): a wrapper styled `white-space: pre`
 * with a monospace font. Its inline styles are syntax-highlight colors, never emphasis.
 */
function isCodeEditorWrapper(doc: Document): boolean {
  const wrapper = doc.body.querySelector<HTMLElement>('[style]');
  if (!wrapper) return false;
  const style = wrapper.style;
  const whiteSpacePre = style.whiteSpace === 'pre' || style.whiteSpace === 'pre-wrap';
  const monospace = /\bmonospace\b/i.test(style.fontFamily);
  return whiteSpacePre && monospace;
}

/**
 * True when the clipboard HTML carries real formatting that should survive the paste — either
 * a semantic element, or style-encoded emphasis. The code-editor signature is an explicit
 * exception to the style clause: its inline styles are syntax colors, never emphasis, so under
 * that signature style-encoded emphasis does not count as formatting.
 */
function htmlCarriesFormatting(html: string): boolean {
  const doc = new DOMParser().parseFromString(html, 'text/html');
  if (doc.body.querySelector(FORMATTING_SELECTOR)) return true;
  if (!isCodeEditorWrapper(doc) && hasStyleEmphasis(doc.body)) return true;
  return false;
}

/**
 * Strip ALL whitespace (incl. newlines). Per-line `<div>`/`<br>` clipboard wrappers (VSCode,
 * terminals) serialize no line separators in textContent, so collapsed-space equality is
 * unachievable for ANY multi-line copy. Flavor agreement is about content, not layout.
 */
function stripWhitespace(s: string): string {
  return s.replace(/\s+/g, '');
}

/**
 * Resolve the clipboard to a verbatim `text/plain` source, or null to fall back to the
 * Turndown path. Two cases yield verbatim:
 *  - the Lore origin marker (lossless Lore→Lore paste), or
 *  - formatting-free external HTML (Notepad/terminal/chat/code-editor wrappers) whose
 *    `text/plain` agrees with the HTML's textContent WHITESPACE-INSENSITIVELY — the class
 *    Turndown would corrupt by escaping markdown metacharacters in prose. Layout (line
 *    breaks, indentation) is taken from plain verbatim, never from the HTML's whitespace
 *    model. On any non-whitespace content mismatch we fall back to Turndown rather than
 *    insert the wrong string.
 */
function resolvePasteSource(html: string, plain: string): string | null {
  if (html.includes(LORE_CLIP_MARK)) {
    return plain || null;
  }
  if (plain && !htmlCarriesFormatting(html)) {
    const doc = new DOMParser().parseFromString(html, 'text/html');
    // WHY: whitespace-insensitive agreement. textContent carries no line separators for
    // per-line-div/<br> wrappers, so collapsed-space equality could never hold for a
    // multi-line copy — every such paste fell through to Turndown and arrived
    // backslash-escaped (regression a326dc2a; original fix c6deb3f4). Stripping \s+ on
    // BOTH sides keeps the gate's intent: only content equality decides verbatim vs
    // Turndown; a real content mismatch still falls back.
    if (stripWhitespace(doc.body.textContent ?? '') === stripWhitespace(plain)) {
      return plain;
    }
  }
  return null;
}

/** True when `pos` sits inside a `FencedCode` node of the markdown syntax tree. */
function inFencedCode(view: EditorView, pos: number): boolean {
  for (let node: SyntaxNode | null = syntaxTree(view.state).resolveInner(pos, -1); node; node = node.parent) {
    if (node.name === 'FencedCode') return true;
  }
  return false;
}

/**
 * Insert `source` at the selection, first upgrading any GFM table to a live table object when
 * a collab handle is active. Shared by the Lore-origin and unformatted-external paths so a
 * pasted GFM table becomes editable, not inert pipe text.
 */
function insertVerbatim(view: EditorView, source: string): void {
  let insert = source;
  // ARCH (split view): the model lands in the TARGET VIEW'S entity ydoc — the focused
  // slot may hold the OTHER column's handle (paste into the reference column must not
  // create the table in the document's ydoc, or vice versa). Unknown views (no
  // registered entity) keep the slot fallback.
  // INVARIANT: the slot fallback is for UNKNOWN views only. Why: a KNOWN entity whose
  // handle has not landed (conn-land race) must degrade to verbatim text — falling
  // through to the slot would import the model into the OTHER column's ydoc while the
  // anchor lands here, splitting model and anchor across ydocs (the exact bug class
  // this targeting exists to prevent).
  const ydoc = entityYdoc(view);
  // WHY: a paste into a fenced code block is code — its pipe table stays text (the same
  // fence skip the scanner applies to fences inside the pasted source).
  if (ydoc && !inFencedCode(view, view.state.selection.main.from)) {
    insert = importGfmTables(ydoc, insert, t('tableBlockUntitled')).text;
  }
  const main = view.state.selection.main;
  view.dispatch({
    changes: { from: main.from, to: main.to, insert },
    selection: { anchor: main.from + insert.length },
  });
}

export const pasteHandlerExtension = EditorView.domEventHandlers({
  paste(e: ClipboardEvent, view) {
    if ((e as unknown as { shiftKey: boolean }).shiftKey) return false;

    const clipboardData = e.clipboardData;
    const html = clipboardData?.getData('text/html');
    if (!html) return false;

    const plain = clipboardData?.getData('text/plain') ?? '';

    // INVARIANT(data-loss): Lore→Lore paste uses the lossless `text/plain` source, never Turndown.
    // Why: Turndown backslash-escapes markdown metacharacters and collapses internal
    // ref: links to visible text, corrupting the copied content. The marker is an HTML
    // comment, invisible to external editors, surviving the standard text/html channel without
    // custom MIME flavors.
    //
    // WHY: formatting-free external clipboard HTML (Notepad/terminal/chat/code-editor
    // wrappers) pastes its `text/plain` source verbatim too, never via Turndown. Why: those
    // sources wrap the plain text in formatting-free `text/html`; Turndown treats it as prose
    // and backslash-escapes #, -, `, _, * everywhere (e.g. `female_look` → `female\_look`),
    // corrupting already-formed text. Verbatim requires the plain to equal the HTML's
    // textContent WHITESPACE-INSENSITIVELY (all \s+ stripped on both sides) so an app
    // writing unrelated content per flavor falls back to Turndown rather than the wrong
    // string, while per-line-div/<br> wrappers — whose textContent has no line separators —
    // still match. A code-editor wrapper (`white-space:pre` + monospace) is an explicit
    // exception: its inline styles are syntax colors, never emphasis.
    const verbatim = resolvePasteSource(html, plain);
    if (verbatim !== null) {
      e.preventDefault();
      insertVerbatim(view, verbatim);
      return true;
    }

    e.preventDefault();
    insertVerbatim(view, htmlToMarkdown(html));
    return true;
  },
});
