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
import TurndownService from 'turndown';
import { gfm } from 'turndown-plugin-gfm';
import { getEntityHandle } from '../collab/active-handle-registry';
import { getActiveHandle, getViewEntity } from './active-editor';
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
    .replace(LEADING_ORDERED_LIST, '$1.');
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

function preprocessTableHtml(html: string): string {
  return html.replace(
    /<(td|th)(?![a-zA-Z])[^>]*>([\s\S]*?)<\/\1>/gi,
    (_match, tag: string, inner: string) => {
      const flat = inner
        .replace(/<br\s*\/?>/gi, ' ')
        .replace(/<\/?p>/gi, ' ')
        .replace(/\s+/g, ' ')
        .trim();
      return `<${tag}>${flat}</${tag}>`;
    },
  );
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
  const entityId = getViewEntity(view);
  const handle = entityId ? getEntityHandle(entityId) : getActiveHandle();
  if (handle?.ydoc) {
    insert = importGfmTables(handle.ydoc, insert, t('tableBlockUntitled')).text;
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
    // ref:/note: links to visible text, corrupting the copied content. The marker is an HTML
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
