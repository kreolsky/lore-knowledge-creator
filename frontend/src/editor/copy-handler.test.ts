// @vitest-environment jsdom
/**
 * Tests for the rich clipboard copy/cut handler.
 *
 * SYSTEM: editor — clipboard copy handler. Drives `handleCopyCut` against a real EditorView
 * (for selection slice + cut dispatch) with a stub ClipboardEvent carrying a Map-backed
 * clipboardData. A stub active handle (ydoc only) exercises the table-anchor→GFM expansion.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { EditorView } from '@codemirror/view';
import { EditorState, EditorSelection } from '@codemirror/state';
import { markdown, markdownLanguage } from '@codemirror/lang-markdown';
import { ensureSyntaxTree } from '@codemirror/language';
import * as Y from 'yjs';
import { handleCopyCut, copyHandlerExtension } from './copy-handler';
import { htmlToMarkdown } from './paste-handler';
import { releaseHandle, publishHandle } from './active-editor';
import type { EntityYjsState } from '../collab/yjs-provider';
import {
  createTable,
  readTableModel,
  tableAnchor,
  modelToGfm,
  TABLE_ANCHOR_RE,
} from '../components/editor/live-preview/table-block-model';
import { importGfmTables } from '../components/editor/live-preview/gfm-table-import';
import { cleanMarkdownToHtml } from '../components/editor/markdown-to-html';

/** Map-backed fake ClipboardEvent so we can read back what the handler wrote. */
function makeClipboardEvent(withData = true): ClipboardEvent {
  if (!withData) return { preventDefault: vi.fn() } as unknown as ClipboardEvent;
  const data = new Map<string, string>();
  return {
    preventDefault: vi.fn(),
    clipboardData: {
      setData: (m: string, v: string) => void data.set(m, v),
      getData: (m: string) => data.get(m) ?? '',
    },
  } as unknown as ClipboardEvent;
}

function makeView(doc: string): EditorView {
  // allowMultipleSelections: the copy handler joins multi-range selections; CM6 collapses
  // them to a single range by default, so opt in for the harness.
  return new EditorView({
    state: EditorState.create({
      doc,
      extensions: [EditorState.allowMultipleSelections.of(true)],
    }),
    parent: document.body,
  });
}

/**
 * Markdown-language-aware view: ligature substitution must classify InlineCode /
 * FencedCode / HorizontalRule nodes, so the test view needs the markdown parser attached
 * (the default `makeView` above is plain text). Mirrors ligature-plugin.test.ts::createView.
 */
function makeMdView(doc: string): EditorView {
  const view = new EditorView({
    state: EditorState.create({
      doc,
      extensions: [
        EditorState.allowMultipleSelections.of(true),
        markdown({ base: markdownLanguage }),
      ],
    }),
    parent: document.body,
  });
  ensureSyntaxTree(view.state, view.state.doc.length, 5000);
  return view;
}

describe('handleCopyCut — prose selection', () => {
  it('writes markdown to text/plain and rendered HTML to text/html', () => {
    const src = '# Title\n\n**bold** text';
    const view = makeView(src);
    view.dispatch({ selection: EditorSelection.range(0, src.length) });
    const e = makeClipboardEvent();
    expect(handleCopyCut(e, view, false)).toBe(true);
    expect(e.preventDefault).toHaveBeenCalled();
    expect(e.clipboardData!.getData('text/plain')).toBe(src);
    expect(e.clipboardData!.getData('text/html')).toContain('<h1>');
    expect(e.clipboardData!.getData('text/html')).toContain('<strong>bold</strong>');
    // CF_HTML-shaped envelope (StartFragment markers wrap the content).
    expect(e.clipboardData!.getData('text/html')).toContain('<!--StartFragment-->');
    expect(e.clipboardData!.getData('text/html')).toContain('<!--EndFragment-->');
    // Lore-origin sentinel lets the paste side choose the lossless text/plain source.
    expect(e.clipboardData!.getData('text/html')).toContain('<!--lore-clipboard-->');
    view.destroy();
  });
});

/**
 * Ligature glyphs land in text/html ONLY — text/plain stays the lossless source (preserves
 * the LORE_CLIP_MARK Lore→Lore lossless-paste invariant). Mirrors `**bold**` → `<strong>`
 * appearing only in the HTML payload.
 */
describe('handleCopyCut — ligature glyphs in text/html only', () => {
  it('substitutes -> and -- in text/html, keeps source in text/plain', () => {
    const src = 'a -> b and c -- d';
    const view = makeMdView(src);
    view.dispatch({ selection: EditorSelection.range(0, src.length) });
    const e = makeClipboardEvent();
    expect(handleCopyCut(e, view, false)).toBe(true);
    // text/plain = lossless source.
    expect(e.clipboardData!.getData('text/plain')).toBe(src);
    // text/html carries the rendered glyphs in the prose (the envelope's `<!--…-->` comment
    // markers also contain `->`, so assert on the rendered paragraph, not the whole payload).
    expect(e.clipboardData!.getData('text/html')).toContain('<p>a \u2192 b and c \u2013 d</p>');
    expect(e.clipboardData!.getData('text/html')).not.toContain('a -> b');
    view.destroy();
  });

  it('keeps -> inside inline code in text/html', () => {
    const src = 'use `->` now';
    const view = makeMdView(src);
    view.dispatch({ selection: EditorSelection.range(0, src.length) });
    const e = makeClipboardEvent();
    handleCopyCut(e, view, false);
    expect(e.clipboardData!.getData('text/plain')).toBe(src);
    // Code span is excluded from substitution → no arrow glyph anywhere; the code span
    // survives as a real <code> element (escapeHtml encodes its `>`, so assert on <code>).
    expect(e.clipboardData!.getData('text/html')).toContain('<code>');
    expect(e.clipboardData!.getData('text/html')).not.toContain('\u2192');
    view.destroy();
  });

  it('keeps a --- horizontal rule from becoming an em-dash in text/html', () => {
    // cleanMarkdownToHtml has no <hr> pass, so the structural --- stays literal in the HTML.
    // The ligature substitution must NOT turn it into — before the HTML conversion.
    const src = 'intro\n\n---\n\nafter';
    const view = makeMdView(src);
    view.dispatch({ selection: EditorSelection.range(0, src.length) });
    const e = makeClipboardEvent();
    handleCopyCut(e, view, false);
    expect(e.clipboardData!.getData('text/plain')).toBe(src);
    expect(e.clipboardData!.getData('text/html')).not.toContain('\u2014');
    view.destroy();
  });
});

describe('handleCopyCut — table selection (anchor → GFM)', () => {
  let ydoc: Y.Doc;
  let anchor: string;

  beforeEach(() => {
    ydoc = new Y.Doc();
    const tableId = createTable(ydoc, [['A', 'B'], ['1', '2']]);
    anchor = tableAnchor('My table', tableId);
    publishHandle({ ydoc } as unknown as EntityYjsState);
  });
  afterEach(() => releaseHandle());

  it('expands the anchor to GFM in text/plain and a real <table> in text/html', () => {
    const view = makeView(anchor);
    view.dispatch({ selection: EditorSelection.range(0, anchor.length) });
    const e = makeClipboardEvent();
    expect(handleCopyCut(e, view, false)).toBe(true);
    expect(e.clipboardData!.getData('text/plain')).toBe('| A | B |\n| --- | --- |\n| 1 | 2 |');
    expect(e.clipboardData!.getData('text/plain')).not.toContain('table:');
    expect(e.clipboardData!.getData('text/html')).toContain('<table>');
    expect(e.clipboardData!.getData('text/html')).toContain('<th>A</th>');
    view.destroy();
  });

  it('keeps the anchor verbatim when no active handle is registered (mount-race degrade)', () => {
    releaseHandle();
    const view = makeView(anchor);
    view.dispatch({ selection: EditorSelection.range(0, anchor.length) });
    const e = makeClipboardEvent();
    handleCopyCut(e, view, false);
    expect(e.clipboardData!.getData('text/plain')).toBe(anchor);
    view.destroy();
  });
});

describe('handleCopyCut — multi-range + cut + guards', () => {
  it('joins multiple selection ranges with a single \\n', () => {
    const view = makeView('first chunk second chunk');
    view.dispatch({
      selection: EditorSelection.create(
        [EditorSelection.range(0, 5), EditorSelection.range(12, 17)],
        0,
      ),
    });
    const e = makeClipboardEvent();
    handleCopyCut(e, view, false);
    expect(e.clipboardData!.getData('text/plain')).toBe('first\nsecon');
    view.destroy();
  });

  it('cut deletes the selection after writing the clipboard', () => {
    const view = makeView('hello world');
    view.dispatch({ selection: EditorSelection.range(0, 5) });
    const e = makeClipboardEvent();
    expect(handleCopyCut(e, view, true)).toBe(true);
    expect(view.state.doc.toString()).toBe(' world');
    view.destroy();
  });

  it('cut with multiple ranges maps the cursor through earlier deletions', () => {
    // 'AAAhelloBBBccc' — cut 'AAA' (range 0) and 'BBB' (main, range 1); 'ccc' stays
    // AFTER the main range so an unmapped anchor would land in-bounds-but-wrong.
    const doc = 'AAAhelloBBBccc';
    const view = makeView(doc);
    view.dispatch({
      selection: EditorSelection.create(
        [EditorSelection.range(0, 3), EditorSelection.range(8, 11)],
        1, // main = the second range (its left edge 8 is preceded by a deleted range)
      ),
    });
    const e = makeClipboardEvent();
    expect(handleCopyCut(e, view, true)).toBe(true);
    // Both ranges deleted; 'ccc' survives after the main range.
    expect(view.state.doc.toString()).toBe('helloccc');
    // Correct post-cut cursor = 8 (pre) − 3 (deleted before it) = 5.
    // A buggy unmapped anchor (8) is valid here (doc len 8) → lands at the end (8).
    expect(view.state.selection.main.anchor).toBe(5);
    view.destroy();
  });

  it('returns false for an all-collapsed selection (lets CM6 default run)', () => {
    const view = makeView('some text'); // default cursor (collapsed) at 0
    const e = makeClipboardEvent();
    expect(handleCopyCut(e, view, false)).toBe(false);
    expect(e.preventDefault).not.toHaveBeenCalled();
    view.destroy();
  });

  it('returns false when clipboardData is missing', () => {
    const view = makeView('some text');
    view.dispatch({ selection: EditorSelection.range(0, 4) });
    const e = makeClipboardEvent(false);
    expect(handleCopyCut(e, view, false)).toBe(false);
    view.destroy();
  });
});

describe('copyHandlerExtension', () => {
  it('is a CM6 Extension (domEventHandlers wiring copy + cut)', () => {
    expect(copyHandlerExtension).toBeTruthy();
  });

  it('intercepts a real copy event on contentDOM and writes both payloads', () => {
    // jsdom's ClipboardEvent.clipboardData is non-functional, so build a bare Event and
    // override clipboardData with a Map-backed mock.
    const data = new Map<string, string>();
    const event = new Event('copy', { bubbles: true, cancelable: true });
    Object.defineProperty(event, 'clipboardData', {
      value: {
        setData: (m: string, v: string) => void data.set(m, v),
        getData: (m: string) => data.get(m) ?? '',
      },
      configurable: true,
    });
    Object.defineProperty(event, 'preventDefault', { value: vi.fn(), configurable: true });

    const view = new EditorView({
      state: EditorState.create({
        doc: '# Hi',
        selection: EditorSelection.range(0, 4),
        extensions: [copyHandlerExtension],
      }),
      parent: document.createElement('div'),
    });
    view.contentDOM.dispatchEvent(event);
    expect(data.get('text/plain')).toBe('# Hi');
    expect(data.get('text/html')).toContain('<h1>Hi</h1>');
    expect(data.get('text/html')).toContain('<!--StartFragment-->');
    view.destroy();
  });
});

/**
 * Copy → paste structural round trip.
 *
 * A table copied in-app expands to GFM, renders to a real `<table>` in text/html, and on
 * paste (Turndown + importGfmTables) is recreated as a fresh table object in the target doc
 * with the right row/column shape. This is the within-app table round trip.
 *
 * KNOWN LIMITATION (asserted as a comment, NOT fixed): the paste path's
 * preprocessTableHtml + tableCellFlatten (paste-handler.ts) collapse `<br>`/newlines to a
 * single space, so MULTILINE cell content loses its line breaks on the copy→paste HTML path.
 * The model↔GFM serializer preserves them; only the Turndown HTML round trip drops them.
 * We therefore use single-line cells here and do not assert multiline-cell fidelity.
 */
describe('copy → paste round trip (table, structural)', () => {
  it('a copied table pastes back as a table object with the same cell shape', () => {
    const ydoc = new Y.Doc();
    const id = createTable(ydoc, [['A', 'B'], ['1', '2']]);
    const model = readTableModel(ydoc, id)!;

    // Copy side: model → GFM → rendered HTML (real <table>).
    const gfm = modelToGfm(model);
    const html = cleanMarkdownToHtml(gfm);
    expect(html).toContain('<table>');
    expect(html).toContain('<th>A</th>');

    // Paste side: HTML → markdown (real Turndown config) → fresh table object.
    const markdown = htmlToMarkdown(html);
    expect(markdown).toContain('A');

    const target = new Y.Doc();
    const { text, count } = importGfmTables(target, markdown, 'Untitled');
    expect(count).toBe(1);
    const newId = [...text.matchAll(TABLE_ANCHOR_RE)][0][2];
    expect(readTableModel(target, newId)!.rows).toEqual([['A', 'B'], ['1', '2']]);
  });
});

/**
 * Prose round trip: block-level clean HTML makes
 * Lore→Lore copy→paste faithful for headings + paragraph breaks + lists. Before the block-layout
 * change this drifted — the `<br/><br/>` inline HTML decoded back to trailing-space hard breaks
 * and glued headings to the next line; with `<p>`/block `<h1>`/real `<ul>` the paragraph
 * structure round-trips byte-identically.
 *
 * List marker spacing (`-   one`, 3 spaces) is Turndown's default listItem padding, NOT block
 * layout — it re-parses as the same list, so we assert the list survives as a valid list rather
 * than forcing byte-identity (Turndown config is shared with external paste; out of scope).
 */
describe('copy → paste round trip (prose, structural)', () => {
  it('headings + paragraph breaks are byte-identical through cleanMarkdownToHtml → htmlToMarkdown', () => {
    const src = '# Title\n\nFirst paragraph.\n\nSecond paragraph.';
    const html = cleanMarkdownToHtml(src);
    expect(html).toBe('<h1>Title</h1><p>First paragraph.</p><p>Second paragraph.</p>');
    expect(html).not.toContain('<br/>');
    expect(htmlToMarkdown(html)).toBe(src);
  });

  it('a list survives the round trip as a valid list (block <ul>, no <br/> glue)', () => {
    const html = cleanMarkdownToHtml('intro\n\n- one\n- two');
    expect(html).toBe('<p>intro</p><ul><li>one</li><li>two</li></ul>');
    const md = htmlToMarkdown(html);
    expect(md).toContain('intro');
    // Turndown pads the marker; both items re-parse as a single list block.
    expect(md).toMatch(/-\s+one/);
    expect(md).toMatch(/-\s+two/);
  });
});
