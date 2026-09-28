// @vitest-environment jsdom
/**
 * Tests for the rich clipboard paste handler.
 *
 * SYSTEM: editor — clipboard paste handler. Drives `pasteHandlerExtension` against a real
 * EditorView with a stub paste event carrying a Map-backed clipboardData. Covers three
 * resolution branches: Lore-origin (marker → lossless text/plain), unformatted external
 * (Notepad/terminal/chat/code-editor wrappers → verbatim text/plain), and formatted
 * external (HTML→Markdown via Turndown, with intraword `_` preserved).
 */

import { describe, it, expect, beforeEach, afterEach, beforeAll, afterAll } from 'vitest';
import { EditorView } from '@codemirror/view';
import { EditorState, EditorSelection } from '@codemirror/state';
import * as Y from 'yjs';
import { pasteHandlerExtension, htmlToMarkdown } from './paste-handler';
import { setEntityHandle } from '../collab/active-handle-registry';
import { releaseHandle, publishHandle, setViewEntity } from './active-editor';
import type { EntityYjsState } from '../collab/yjs-provider';
import { readTableModel, getTablesMap, TABLE_ANCHOR_RE } from '../components/editor/live-preview/table-block-model';

const origRangeGetClientRects = Range.prototype.getClientRects;
const origRangeGetBoundingClientRect = Range.prototype.getBoundingClientRect;

beforeAll(() => {
  Range.prototype.getClientRects = function () {
    return { length: 0, item: () => null } as unknown as DOMRectList;
  };
  Range.prototype.getBoundingClientRect = function () {
    return { top: 0, left: 0, bottom: 0, right: 0, width: 0, height: 0, x: 0, y: 0, toJSON: () => ({}) } as DOMRect;
  };
});

afterAll(() => {
  Range.prototype.getClientRects = origRangeGetClientRects;
  Range.prototype.getBoundingClientRect = origRangeGetBoundingClientRect;
});

function makeView(doc = ''): EditorView {
  return new EditorView({
    state: EditorState.create({
      doc,
      selection: EditorSelection.cursor(0),
      extensions: [pasteHandlerExtension],
    }),
    parent: document.body,
  });
}

function makePasteEvent({
  html = '',
  plain = '',
  shift = false,
}: {
  html?: string;
  plain?: string;
  shift?: boolean;
}): Event {
  const data = new Map<string, string>();
  if (html) data.set('text/html', html);
  if (plain) data.set('text/plain', plain);

  const event = new Event('paste', { bubbles: true, cancelable: true });
  Object.defineProperty(event, 'clipboardData', {
    value: {
      setData: (m: string, v: string) => void data.set(m, v),
      getData: (m: string) => data.get(m) ?? '',
    },
    configurable: true,
  });
  Object.defineProperty(event, 'shiftKey', { value: shift, configurable: true });
  return event;
}

describe('pasteHandlerExtension', () => {
  it('is a CM6 extension (domEventHandlers wiring paste)', () => {
    expect(pasteHandlerExtension).toBeTruthy();
  });
});

describe('Lore-origin paste (lossless source path)', () => {
  let ydoc: Y.Doc;

  beforeEach(() => {
    ydoc = new Y.Doc();
    publishHandle({ ydoc } as unknown as EntityYjsState);
  });
  afterEach(() => releaseHandle());

  it('inserts ref:/note: links and underscores without Turndown escaping', () => {
    const source = '[src](ref:abc) and my_var [n](note:n1)';
    const view = makeView();
    view.contentDOM.dispatchEvent(
      makePasteEvent({ html: '<!--lore-clipboard--><p>ignored</p>', plain: source }),
    );
    const doc = view.state.doc.toString();
    expect(doc).toContain('[src](ref:abc)');
    expect(doc).toContain('my_var');
    expect(doc).not.toContain('\\_');
    expect(doc).toContain('[n](note:n1)');
    view.destroy();
  });

  it('recreates a GFM table as a table object, not raw pipe text', () => {
    const source = '| A | B |\n| --- | --- |\n| 1 | 2 |';
    const view = makeView();
    view.contentDOM.dispatchEvent(
      makePasteEvent({ html: '<!--lore-clipboard--><table></table>', plain: source }),
    );
    const doc = view.state.doc.toString();
    const matches = [...doc.matchAll(TABLE_ANCHOR_RE)];
    expect(matches).toHaveLength(1);
    const id = matches[0][2];
    expect(readTableModel(ydoc, id)!.rows).toEqual([
      ['A', 'B'],
      ['1', '2'],
    ]);
    view.destroy();
  });

  it('preserves bare document ids and doc: scheme links byte-for-byte', () => {
    const source = '[title](docId) and [title](doc:docId)';
    const view = makeView();
    view.contentDOM.dispatchEvent(
      makePasteEvent({ html: '<!--lore-clipboard--><p>x</p>', plain: source }),
    );
    expect(view.state.doc.toString()).toBe(source);
    view.destroy();
  });

  it('preserves inline code and fenced code containing markdown metacharacters', () => {
    const source = '`my_var * [x]`\n\n```\n_var_ [a] `b`\n```';
    const view = makeView();
    view.contentDOM.dispatchEvent(
      makePasteEvent({ html: '<!--lore-clipboard--><p>ignored</p>', plain: source }),
    );
    const doc = view.state.doc.toString();
    expect(doc).toBe(source);
    expect(doc).not.toContain('\\_');
    expect(doc).not.toContain('\\[');
    expect(doc).not.toContain('\\`');
    view.destroy();
  });

  it('inserts raw source when no collab handle is active', () => {
    releaseHandle();
    const source = '[src](ref:abc) my_var';
    const view = makeView();
    view.contentDOM.dispatchEvent(
      makePasteEvent({ html: '<!--lore-clipboard--><p>ignored</p>', plain: source }),
    );
    expect(view.state.doc.toString()).toBe(source);
    view.destroy();
  });
});

describe('Unformatted external paste (verbatim text/plain path)', () => {
  beforeEach(() => releaseHandle());
  afterEach(() => releaseHandle());

  // The class this fix targets: Notepad/terminal/chat put a formatting-free text/html
  // wrapper alongside text/plain. Every markdown metacharacter must land byte-for-byte,
  // not backslash-escaped by Turndown.
  it.each([
    'female_look_chemical_suit_aug_2026',
    'arr[0]',
    '2 * 3',
    '# heading',
  ])('pastes notepad-style <div> "%s" verbatim (no Turndown escaping)', (source) => {
    const view = makeView();
    view.contentDOM.dispatchEvent(makePasteEvent({ html: `<div>${source}</div>`, plain: source }));
    expect(view.state.doc.toString()).toBe(source);
    view.destroy();
  });

  it('falls back to Turndown when text/plain is empty (formatted HTML wins)', () => {
    const view = makeView();
    view.contentDOM.dispatchEvent(makePasteEvent({ html: '<strong>b</strong>', plain: '' }));
    expect(view.state.doc.toString()).toBe('**b**');
    view.destroy();
  });

  it('falls back to Turndown when text/plain is absent', () => {
    const view = makeView();
    view.contentDOM.dispatchEvent(makePasteEvent({ html: '<strong>b</strong>' }));
    expect(view.state.doc.toString()).toBe('**b**');
    view.destroy();
  });

  it('falls back to Turndown of the HTML when plain disagrees with textContent', () => {
    // HTML textContent is `2 * 3`; plain is unrelated → do not insert the wrong string.
    const view = makeView();
    view.contentDOM.dispatchEvent(
      makePasteEvent({ html: '<div>2 * 3</div>', plain: 'different payload' }),
    );
    expect(view.state.doc.toString()).toBe('2 \\* 3');
    view.destroy();
  });

  it('treats style-encoded bold (font-weight:700) as formatting → Turndown path', () => {
    // Word/Gmail encode bold as inline style, not a <strong> tag.
    const view = makeView();
    view.contentDOM.dispatchEvent(
      makePasteEvent({ html: '<span style="font-weight:700">2 * 3</span>', plain: '2 * 3' }),
    );
    expect(view.state.doc.toString()).toBe('2 \\* 3');
    view.destroy();
  });
});

describe('Unformatted GFM table on the verbatim path', () => {
  let ydoc: Y.Doc;

  beforeEach(() => {
    ydoc = new Y.Doc();
    publishHandle({ ydoc } as unknown as EntityYjsState);
  });
  afterEach(() => releaseHandle());

  it('upgrades an unformatted GFM table to a live table object, not inert pipe text', () => {
    const source = '| A | B |\n| --- | --- |\n| 1 | 2 |';
    const view = makeView();
    view.contentDOM.dispatchEvent(
      makePasteEvent({ html: `<div>${source}</div>`, plain: source }),
    );
    const doc = view.state.doc.toString();
    const matches = [...doc.matchAll(TABLE_ANCHOR_RE)];
    expect(matches).toHaveLength(1);
    const id = matches[0][2];
    expect(readTableModel(ydoc, id)!.rows).toEqual([
      ['A', 'B'],
      ['1', '2'],
    ]);
    view.destroy();
  });
});

describe('Code-editor copy (VSCode signature)', () => {
  beforeEach(() => releaseHandle());
  afterEach(() => releaseHandle());

  it('pastes a VSCode copy (white-space:pre + monospace) verbatim, unescaped', () => {
    const source = 'female_look_chemical_suit_aug_2026';
    const view = makeView();
    view.contentDOM.dispatchEvent(
      makePasteEvent({
        html: `<div style="white-space: pre; font-family: monospace;"><span>${source}</span></div>`,
        plain: source,
      }),
    );
    expect(view.state.doc.toString()).toBe(source);
    view.destroy();
  });

  it('treats a code-editor wrapper with bold style as unformatted (style-clause exception)', () => {
    // Without the exception, font-weight:bold would mark it formatted → Turndown → `2 \* 3`.
    // The pre+monospace signature means inline styles are syntax colors, never emphasis.
    const source = '2 * 3';
    const view = makeView();
    view.contentDOM.dispatchEvent(
      makePasteEvent({
        html: `<div style="white-space: pre; font-family: monospace; font-weight: bold;"><span>${source}</span></div>`,
        plain: source,
      }),
    );
    expect(view.state.doc.toString()).toBe('2 * 3');
    view.destroy();
  });
});

describe('Multi-line code-editor copy (per-line <div> / <br> wrappers)', () => {
  let ydoc: Y.Doc;

  beforeEach(() => {
    ydoc = new Y.Doc();
    publishHandle({ ydoc } as unknown as EntityYjsState);
  });
  afterEach(() => releaseHandle());

  // Real VSCode clipboard HTML (copyWithSyntaxHighlighting, default font, Dark theme):
  // one <div> per source line, empty line = <div><br></div>, NO literal newlines between
  // divs — textContent concatenates lines without separators. Syntax tokens are color
  // spans; a font-weight:bold span (markdown preview) pins the style-emphasis exemption.
  const wrapperStyle =
    "color: #d4d4d4;background-color: #1e1e1e;font-family: Menlo, Monaco, 'Courier New', monospace;font-weight: normal;font-size: 12px;line-height: 18px;white-space: pre;";
  const multilineSource = [
    '# Title',
    '',
    '## Section with **bold**',
    '',
    '- item one',
    '- item with `code`',
    '',
    '| A | B |',
    '| --- | --- |',
    '| 1 | 2 |',
  ].join('\n');
  const vscodeHtml =
    `<div style="${wrapperStyle}">` +
    '<div># Title</div>' +
    '<div><br></div>' +
    '<div><span style="color: #569cd6;">## Section with </span><span style="font-weight: bold;">**bold**</span></div>' +
    '<div><br></div>' +
    '<div>- item one</div>' +
    '<div>- item with <span style="color: #ce9178;">`code`</span></div>' +
    '<div><br></div>' +
    '<div>| A | B |</div>' +
    '<div>| --- | --- |</div>' +
    '<div>| 1 | 2 |</div>' +
    '</div>';

  it('pastes multi-line markdown verbatim, not Turndown-escaped (regression pin)', () => {
    releaseHandle();
    const view = makeView();
    view.contentDOM.dispatchEvent(makePasteEvent({ html: vscodeHtml, plain: multilineSource }));
    const doc = view.state.doc.toString();
    expect(doc).toBe(multilineSource);
    expect(doc).not.toContain('\\#');
    expect(doc).not.toContain('\\*');
    expect(doc).not.toContain('\\`');
    view.destroy();
  });

  it('upgrades the embedded GFM table to a live table object on the verbatim path', () => {
    const view = makeView();
    view.contentDOM.dispatchEvent(makePasteEvent({ html: vscodeHtml, plain: multilineSource }));
    const doc = view.state.doc.toString();
    const matches = [...doc.matchAll(TABLE_ANCHOR_RE)];
    expect(matches).toHaveLength(1);
    const id = matches[0][2];
    expect(readTableModel(ydoc, id)!.rows).toEqual([
      ['A', 'B'],
      ['1', '2'],
    ]);
    view.destroy();
  });

  it('pastes a terminal-style <br>-joined multi-line copy verbatim', () => {
    releaseHandle();
    const source = '# term heading\n**bold** line';
    const html =
      '<div style="white-space: pre; font-family: monospace;"># term heading<br>**bold** line</div>';
    const view = makeView();
    view.contentDOM.dispatchEvent(makePasteEvent({ html, plain: source }));
    expect(view.state.doc.toString()).toBe(source);
    view.destroy();
  });

  it('inserts plain verbatim when the flavors differ only in whitespace', () => {
    // Documents the loosened gate: whitespace divergence alone never routes to Turndown.
    releaseHandle();
    const view = makeView();
    view.contentDOM.dispatchEvent(makePasteEvent({ html: '<div>a  b</div>', plain: 'a b' }));
    expect(view.state.doc.toString()).toBe('a b');
    view.destroy();
  });
});

describe('External formatted paste (Turndown path)', () => {
  beforeEach(() => releaseHandle());
  afterEach(() => releaseHandle());

  it('converts <strong> to ** when no Lore marker is present', () => {
    const view = makeView();
    view.contentDOM.dispatchEvent(makePasteEvent({ html: '<strong>b</strong>' }));
    expect(view.state.doc.toString()).toBe('**b**');
    view.destroy();
  });

  it('falls back to Turndown when the marker is present but text/plain is empty', () => {
    const view = makeView();
    view.contentDOM.dispatchEvent(
      makePasteEvent({ html: '<!--lore-clipboard--><strong>b</strong>', plain: '' }),
    );
    expect(view.state.doc.toString()).toBe('**b**');
    view.destroy();
  });

  it('falls back to Turndown when the marker is present but text/plain is absent', () => {
    const view = makeView();
    view.contentDOM.dispatchEvent(
      makePasteEvent({ html: '<!--lore-clipboard--><em>x</em>' }),
    );
    expect(view.state.doc.toString()).toBe('*x*');
    view.destroy();
  });

  it('does not escape a style-bold "1. Привет" (ordered-list literal on the live paste path)', () => {
    const view = makeView();
    view.contentDOM.dispatchEvent(
      makePasteEvent({ html: '<p style="font-weight:700">1. Привет</p>', plain: '1. Привет' }),
    );
    expect(view.state.doc.toString()).toBe('1. Привет');
    view.destroy();
  });

  it('does not escape [[wikilinks]] inside <strong> on the live paste path', () => {
    const view = makeView();
    view.contentDOM.dispatchEvent(
      makePasteEvent({
        html: '<p><strong>Спецификация [[wikilinks]]</strong></p>',
        plain: 'Спецификация [[wikilinks]]',
      }),
    );
    expect(view.state.doc.toString()).toBe('**Спецификация [[wikilinks]]**');
    view.destroy();
  });
});

describe('Turndown intraword underscore', () => {
  it('keeps <strong> emphasis while leaving intraword _ unescaped', () => {
    expect(htmlToMarkdown('<p><strong>a</strong> x_y_z</p>')).toBe('**a** x_y_z');
  });

  it('still escapes flanking underscores that are emphasis delimiters', () => {
    expect(htmlToMarkdown('<p>_italic_</p>')).toBe('\\_italic\\_');
  });

  it('does not touch underscores inside code (escape is never called there)', () => {
    expect(htmlToMarkdown('<pre><code>a_b_c</code></pre>')).toBe('```\na_b_c\n```');
  });
});

describe('Turndown ordered-list literal', () => {
  it('keeps a line-start "1. " unescaped (the editor renders ordered lists natively)', () => {
    expect(htmlToMarkdown('<p>1. Привет</p>')).toBe('1. Привет');
  });

  it('keeps "1. " unescaped inside <strong> wrapping', () => {
    expect(htmlToMarkdown('<p><strong>1. Привет</strong></p>')).toBe('**1. Привет**');
  });

  it('leaves a real <ol> list untouched (regression guard)', () => {
    const md = htmlToMarkdown('<ol><li>Привет</li></ol>');
    expect(md).toContain('1.');
    expect(md).not.toContain('\\.');
  });

  it('does not unescape a mid-sentence "1. " (only the line start is a list marker)', () => {
    expect(htmlToMarkdown('<p>see 1. text here</p>')).toBe('see 1. text here');
  });

  it('unescapes "1. " that starts a line after <br>', () => {
    const md = htmlToMarkdown('<p>intro<br>1. text</p>');
    expect(md).toContain('1. text');
    expect(md).not.toContain('1\\.');
  });
});

describe('Turndown literal brackets', () => {
  it('keeps [[wikilinks]] literal', () => {
    expect(htmlToMarkdown('<p>see [[wikilinks]] here</p>')).toBe('see [[wikilinks]] here');
  });

  it('keeps [[wikilinks]] literal inside <strong> (the exact user-reported case)', () => {
    expect(htmlToMarkdown('<p><strong>Спецификация [[wikilinks]]</strong></p>')).toBe(
      '**Спецификация [[wikilinks]]**',
    );
  });

  it('keeps lone array/citation brackets literal', () => {
    expect(htmlToMarkdown('<p>a[0] b</p>')).toBe('a[0] b');
    expect(htmlToMarkdown('<p>ref [1] end</p>')).toBe('ref [1] end');
  });

  it('still converts a real <a> into a markdown link (link rule, not escape)', () => {
    expect(htmlToMarkdown('<p><a href="u">text</a></p>')).toBe('[text](u)');
  });

  it('treats a literal link-pattern as a live link (documented side effect)', () => {
    expect(htmlToMarkdown('<p>[текст](u)</p>')).toBe('[текст](u)');
  });

  it('preserves a user backslash-bracket (off-by-one guard for the shield)', () => {
    // Source markdown literal "\[x" carries its own backslash; Turndown must double the
    // backslash (escape) but NOT then escape the bracket. Output renders as the literal "\[x".
    expect(htmlToMarkdown('<p>\\[x</p>')).toBe('\\\\[x');
  });
});

describe('Paste guards', () => {
  beforeEach(() => releaseHandle());
  afterEach(() => releaseHandle());

  it('pastes plain text when Shift is held (browser default, strips formatting)', () => {
    const view = makeView();
    const event = makePasteEvent({
      html: '<!--lore-clipboard--><p>ignored</p>',
      plain: 'shift plain',
      shift: true,
    });
    view.contentDOM.dispatchEvent(event);
    expect(view.state.doc.toString()).toBe('shift plain');
    view.destroy();
  });
});

describe('Paste table targeting (split view)', () => {
  afterEach(() => {
    releaseHandle();
    setEntityHandle('e-1', null);
  });

  const GFM = '| A | B |\n| --- | --- |\n| 1 | 2 |';

  it('creates the model in the VIEW\'S entity ydoc while the slot points at a foreign one', () => {
    const entityDoc = new Y.Doc();
    setEntityHandle('e-1', { ydoc: entityDoc } as unknown as EntityYjsState);
    const foreignDoc = new Y.Doc();
    publishHandle({ ydoc: foreignDoc } as unknown as EntityYjsState);

    const view = makeView();
    setViewEntity(view, 'e-1');
    view.contentDOM.dispatchEvent(
      makePasteEvent({ html: '<!--lore-clipboard--><table></table>', plain: GFM }),
    );

    // Anchor landed in the view's doc; the model in the ENTITY ydoc…
    const matches = [...view.state.doc.toString().matchAll(TABLE_ANCHOR_RE)];
    expect(matches).toHaveLength(1);
    expect(readTableModel(entityDoc, matches[0][2])!.rows).toEqual([
      ['A', 'B'],
      ['1', '2'],
    ]);
    // …and the foreign (slot) ydoc got nothing.
    expect([...getTablesMap(foreignDoc).keys()]).toHaveLength(0);
    view.destroy();
  });

  it('unknown view → old fallback: the slot handle is used', () => {
    const slotDoc = new Y.Doc();
    publishHandle({ ydoc: slotDoc } as unknown as EntityYjsState);

    const view = makeView(); // NOT registered for any entity
    view.contentDOM.dispatchEvent(
      makePasteEvent({ html: '<!--lore-clipboard--><table></table>', plain: GFM }),
    );

    const matches = [...view.state.doc.toString().matchAll(TABLE_ANCHOR_RE)];
    expect(matches).toHaveLength(1);
    expect(readTableModel(slotDoc, matches[0][2])!.rows).toEqual([
      ['A', 'B'],
      ['1', '2'],
    ]);
    view.destroy();
  });

  it('known entity whose handle has NOT landed → verbatim text, never the slot ydoc', () => {
    // The conn-land race: the view IS registered for e-1, but e-1's handle is not
    // published yet. The slot holds the OTHER column's live handle. Falling back to
    // the slot would put the model in the foreign ydoc and the anchor in this view —
    // the exact split-ydoc split this targeting exists to prevent.
    const foreignDoc = new Y.Doc();
    publishHandle({ ydoc: foreignDoc } as unknown as EntityYjsState);

    const view = makeView();
    setViewEntity(view, 'e-1');
    view.contentDOM.dispatchEvent(
      makePasteEvent({ html: '<!--lore-clipboard--><table></table>', plain: GFM }),
    );

    // Degrade: the GFM source is inserted as plain text — no anchor, no import.
    expect(view.state.doc.toString()).toContain('| A | B |');
    expect([...view.state.doc.toString().matchAll(TABLE_ANCHOR_RE)]).toHaveLength(0);
    expect([...getTablesMap(foreignDoc).keys()]).toHaveLength(0);
    view.destroy();
  });
});
