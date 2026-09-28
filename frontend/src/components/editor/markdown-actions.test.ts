/** Unit tests for markdown formatting actions — CM6 EditorView in jsdom. */

// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { markdown, markdownLanguage } from '@codemirror/lang-markdown';
import { indentUnit, ensureSyntaxTree, syntaxTree } from '@codemirror/language';
import { markdownActionRegistry } from './markdown-actions';
import { on, off } from '../../events';

// ── Helpers ──────────────────────────────────────────────────────────────────

function createView(doc: string, anchor: number, head?: number): EditorView {
  const state = EditorState.create({
    doc,
    selection: { anchor, head: head ?? anchor },
  });
  // WHY: jsdom lacks getClientRects on Range — stub it so coordsAtPos doesn't throw.
  const origCreateRange = document.createRange.bind(document);
  document.createRange = () => {
    const range = origCreateRange();
    if (!range.getClientRects) {
      range.getClientRects = () => ({ length: 0, item: () => null } as unknown as DOMRectList);
    }
    if (!range.getBoundingClientRect) {
      range.getBoundingClientRect = () => ({ top: 0, left: 0, bottom: 0, right: 0, width: 0, height: 0, x: 0, y: 0, toJSON: () => ({}) } as DOMRect);
    }
    return range;
  };
  return new EditorView({ state, parent: document.createElement('div') });
}

function docText(view: EditorView): string {
  return view.state.doc.toString();
}

function sel(view: EditorView) {
  const { from, to } = view.state.selection.main;
  return { from, to };
}

// ── Setup ────────────────────────────────────────────────────────────────────

let emittedEvents: { type: string; payload: any }[] = [];
const captureHandlers: Record<string, (payload: any) => void> = {};

function captureEvent(type: string) {
  const handler = (payload: any) => emittedEvents.push({ type, payload });
  captureHandlers[type] = handler;
  on(type as any, handler);
}

function stopCapture(type: string) {
  if (captureHandlers[type]) {
    off(type as any, captureHandlers[type]);
    delete captureHandlers[type];
  }
}

beforeEach(() => {
  emittedEvents = [];
  captureEvent('show-link-suggestions');
  captureEvent('open-notes');
  captureEvent('open-find');
  captureEvent('create-note-from-editor');
});

afterEach(() => {
  stopCapture('show-link-suggestions');
  stopCapture('open-notes');
  stopCapture('open-find');
  stopCapture('create-note-from-editor');
  vi.restoreAllMocks();
});

// ── toggleInlineMarker (bold) ────────────────────────────────────────────────

describe('bold (toggleInlineMarker **)', () => {
  it('wraps surrounding word at empty caret inside word', () => {
    const view = createView('hello', 3);
    markdownActionRegistry.bold(view);
    expect(docText(view)).toBe('**hello**');
    expect(sel(view)).toEqual({ from: 5, to: 5 });
  });

  it('no-op at empty caret in whitespace', () => {
    const view = createView('', 0);
    markdownActionRegistry.bold(view);
    expect(docText(view)).toBe('');
    expect(sel(view)).toEqual({ from: 0, to: 0 });
  });

  it('wraps selected text', () => {
    const view = createView('hello world', 0, 5);
    markdownActionRegistry.bold(view);
    expect(docText(view)).toBe('**hello** world');
  });

  it('unwraps already wrapped selection', () => {
    const view = createView('**hello**', 0, 9);
    markdownActionRegistry.bold(view);
    expect(docText(view)).toBe('hello');
    expect(sel(view)).toEqual({ from: 0, to: 5 });
  });

  it('unwraps outer markers when inner text selected', () => {
    const view = createView('**hello**', 2, 7);
    markdownActionRegistry.bold(view);
    expect(docText(view)).toBe('hello');
    expect(sel(view)).toEqual({ from: 0, to: 5 });
  });

  it('selection spans full wrapped text after wrap', () => {
    const view = createView('abc', 0, 3);
    markdownActionRegistry.bold(view);
    expect(docText(view)).toBe('**abc**');
    expect(sel(view)).toEqual({ from: 0, to: 7 });
  });
});

// ── toggleInlineMarker (italic) ──────────────────────────────────────────────

describe('italic (toggleInlineMarker *)', () => {
  it('wraps selected text', () => {
    const view = createView('hello', 0, 5);
    markdownActionRegistry.italic(view);
    expect(docText(view)).toBe('*hello*');
  });

  it('unwraps already wrapped selection', () => {
    const view = createView('*hello*', 0, 7);
    markdownActionRegistry.italic(view);
    expect(docText(view)).toBe('hello');
  });

  it('no-op at empty caret in whitespace', () => {
    const view = createView('', 0);
    markdownActionRegistry.italic(view);
    expect(docText(view)).toBe('');
    expect(sel(view)).toEqual({ from: 0, to: 0 });
  });

  it('wraps surrounding word at empty caret inside word', () => {
    const view = createView('hello', 3);
    markdownActionRegistry.italic(view);
    expect(docText(view)).toBe('*hello*');
    expect(sel(view)).toEqual({ from: 4, to: 4 });
  });
});

// ── toggleInlineMarker (strikethrough) ───────────────────────────────────────

describe('strikethrough (toggleInlineMarker ~~)', () => {
  it('wraps selected text', () => {
    const view = createView('hello', 0, 5);
    markdownActionRegistry.strikethrough(view);
    expect(docText(view)).toBe('~~hello~~');
  });

  it('unwraps already wrapped selection', () => {
    const view = createView('~~hello~~', 0, 9);
    markdownActionRegistry.strikethrough(view);
    expect(docText(view)).toBe('hello');
  });

  it('unwraps outer markers when inner text selected', () => {
    const view = createView('~~hello~~', 2, 7);
    markdownActionRegistry.strikethrough(view);
    expect(docText(view)).toBe('hello');
  });
});

// ── toggleInlineMarker (inlineCode) ──────────────────────────────────────────

describe('inlineCode (toggleInlineMarker `)', () => {
  it('wraps selected text', () => {
    const view = createView('hello', 0, 5);
    markdownActionRegistry.inlineCode(view);
    expect(docText(view)).toBe('`hello`');
  });

  it('unwraps already wrapped selection', () => {
    const view = createView('`hello`', 0, 7);
    markdownActionRegistry.inlineCode(view);
    expect(docText(view)).toBe('hello');
  });
});

// ── setHeading ───────────────────────────────────────────────────────────────

describe('heading1 (setHeading)', () => {
  it('adds heading prefix to plain line', () => {
    const view = createView('Hello', 0);
    markdownActionRegistry.heading1(view);
    expect(docText(view)).toBe('# Hello');
  });

  it('toggles off same-level heading', () => {
    const view = createView('# Hello', 2);
    markdownActionRegistry.heading1(view);
    expect(docText(view)).toBe('Hello');
  });

  it('replaces different-level heading', () => {
    const view = createView('## Hello', 3);
    markdownActionRegistry.heading1(view);
    expect(docText(view)).toBe('# Hello');
  });

  it('heading2 adds ## prefix', () => {
    const view = createView('Hello', 0);
    markdownActionRegistry.heading2(view);
    expect(docText(view)).toBe('## Hello');
  });

  it('heading3 replaces # with ###', () => {
    const view = createView('# Hello', 2);
    markdownActionRegistry.heading3(view);
    expect(docText(view)).toBe('### Hello');
  });

  it('heading4 adds #### prefix', () => {
    const view = createView('Hello', 0);
    markdownActionRegistry.heading4(view);
    expect(docText(view)).toBe('#### Hello');
  });
});

// ── indentLines ──────────────────────────────────────────────────────────────

describe('indent', () => {
  it('prepends tab to single line', () => {
    const view = createView('hello', 0);
    markdownActionRegistry.indent(view);
    expect(docText(view)).toBe('\thello');
  });

  it('prepends tab to each line in selection', () => {
    const view = createView('line1\nline2\nline3', 0, 17);
    markdownActionRegistry.indent(view);
    expect(docText(view)).toBe('\tline1\n\tline2\n\tline3');
  });
});

// ── indent on checkbox bullets (list-aware) ──────────────────────────────────

function createMarkdownView(doc: string, anchor: number, head?: number): EditorView {
  const state = EditorState.create({
    doc,
    selection: { anchor, head: head ?? anchor },
    extensions: [
      markdown({ base: markdownLanguage, addKeymap: false }),
      indentUnit.of('\t'),
    ],
  });
  const origCreateRange = document.createRange.bind(document);
  document.createRange = () => {
    const range = origCreateRange();
    if (!range.getClientRects) {
      range.getClientRects = () => ({ length: 0, item: () => null } as unknown as DOMRectList);
    }
    if (!range.getBoundingClientRect) {
      range.getBoundingClientRect = () => ({ top: 0, left: 0, bottom: 0, right: 0, width: 0, height: 0, x: 0, y: 0, toJSON: () => ({}) } as DOMRect);
    }
    return range;
  };
  const view = new EditorView({ state, parent: document.createElement('div') });
  ensureSyntaxTree(view.state, view.state.doc.length, 5000);
  return view;
}

function hasTaskMarkerOnLine(view: EditorView, lineNumber: number): boolean {
  ensureSyntaxTree(view.state, view.state.doc.length, 5000);
  const line = view.state.doc.line(lineNumber);
  let found = false;
  syntaxTree(view.state).iterate({
    from: line.from,
    to: line.to,
    enter(node) {
      if (node.name === 'TaskMarker') found = true;
    },
  });
  return found;
}

describe('indent on checkbox lines preserves TaskMarker', () => {
  // The original bug: pressing Tab on a fresh empty checkbox bullet (created via
  // Enter, no trailing space) demoted the line out of the task list. Root cause
  // was the missing trailing space after `[ ]` — the Lezer task-list grammar
  // requires it. The fix lives in list-continuation (always-trailing-space);
  // these tests guarantee that with the trailing space, indent → nested Task.
  it('1 tab nests checkbox under checkbox parent (TaskMarker survives)', () => {
    const view = createMarkdownView('- [ ] foo\n- [ ] ', 16);
    markdownActionRegistry.indent(view);
    expect(docText(view)).toBe('- [ ] foo\n\t- [ ] ');
    expect(hasTaskMarkerOnLine(view, 2)).toBe(true);
  });

  it('1 tab nests filled checkbox under checkbox parent', () => {
    const view = createMarkdownView('- [ ] foo\n- [ ] bar', 19);
    markdownActionRegistry.indent(view);
    expect(docText(view)).toBe('- [ ] foo\n\t- [ ] bar');
    expect(hasTaskMarkerOnLine(view, 2)).toBe(true);
  });

  it('outdent reverses checkbox indent', () => {
    const view = createMarkdownView('- [ ] foo\n\t- [ ] ', 17);
    markdownActionRegistry.outdent(view);
    expect(docText(view)).toBe('- [ ] foo\n- [ ] ');
  });

  // Regression: empty checkbox + Tab/Cmd+] on the LAST line of the document
  // (no content below). The naive `anchor: from + 1` arithmetic in indentLines
  // landed the cursor on the right boundary of the now-grown CheckboxWidget
  // decoration, where Firefox/EOF parser shape decided the cursor side
  // unpredictably. Cursor preservation relative to line.to fixes it.
  it('Tab on empty checkbox at EOF — cursor stays at new line.to', () => {
    // The reported bug: cursor jumped to before `-`. Cursor preservation via
    // `preserveCursorFromLineEnd` keeps the cursor at the new line.to so
    // it never lands on the CheckboxWidget boundary.
    // (Lezer's TaskMarker recognition at bare EOF is its own parser quirk —
    // out of scope per plan; once the cursor stays at line.to the widget
    // re-resolves on the next keystroke.)
    const view = createMarkdownView('- [ ] ', 6);
    markdownActionRegistry.indent(view);
    expect(docText(view)).toBe('\t- [ ] ');
    expect(sel(view)).toEqual({ from: 7, to: 7 });
  });

  it('Tab on empty checkbox followed by content — TaskMarker survives', () => {
    const view = createMarkdownView('- [ ] foo\n- [ ] ', 16);
    markdownActionRegistry.indent(view);
    expect(docText(view)).toBe('- [ ] foo\n\t- [ ] ');
    expect(hasTaskMarkerOnLine(view, 2)).toBe(true);
    expect(sel(view)).toEqual({ from: 17, to: 17 });
  });

  it('Cmd+[ on empty indented checkbox at EOF — cursor stays at new line.to', () => {
    const view = createMarkdownView('\t- [ ] ', 7);
    markdownActionRegistry.outdent(view);
    expect(docText(view)).toBe('- [ ] ');
    expect(sel(view)).toEqual({ from: 6, to: 6 });
  });

  it('Tab on non-empty checkbox preserves cursor offset from line.to', () => {
    // line: `- [ ] foo` (length 9), cursor between `fo` and `o` (pos 8).
    // Offset from line.to: 9 - 8 = 1. After Tab → `\t- [ ] foo` (length 10),
    // cursor should land at 10 - 1 = 9 (still between `fo` and `o`).
    const view = createMarkdownView('- [ ] foo', 8);
    markdownActionRegistry.indent(view);
    expect(docText(view)).toBe('\t- [ ] foo');
    expect(sel(view)).toEqual({ from: 9, to: 9 });
  });
});

// ── outdentLines ─────────────────────────────────────────────────────────────

describe('outdent', () => {
  it('removes leading tab', () => {
    const view = createView('\thello', 1);
    markdownActionRegistry.outdent(view);
    expect(docText(view)).toBe('hello');
  });

  it('removes up to 4 leading spaces', () => {
    const view = createView('    hello', 4);
    markdownActionRegistry.outdent(view);
    expect(docText(view)).toBe('hello');
  });

  it('removes only up to 4 spaces (leaves remainder)', () => {
    const view = createView('      hello', 6);
    markdownActionRegistry.outdent(view);
    expect(docText(view)).toBe('  hello');
  });

  it('no-op when no indent', () => {
    const view = createView('hello', 0);
    const result = markdownActionRegistry.outdent(view);
    expect(result).toBe(true);
    expect(docText(view)).toBe('hello');
  });

  it('removes indent from multiple lines', () => {
    const view = createView('\tline1\n\tline2', 0, 13);
    markdownActionRegistry.outdent(view);
    expect(docText(view)).toBe('line1\nline2');
  });

  it('clamps cursor to line start after removal', () => {
    const view = createView('\thello', 1, 6);
    markdownActionRegistry.outdent(view);
    const { from } = sel(view);
    expect(from).toBe(0);
  });

  it('preserves top-level dash bullet marker (no indent to strip)', () => {
    const view = createView('- hello', 2);
    markdownActionRegistry.outdent(view);
    expect(docText(view)).toBe('- hello');
  });

  it('preserves top-level asterisk bullet marker (no indent to strip)', () => {
    const view = createView('* hello', 2);
    markdownActionRegistry.outdent(view);
    expect(docText(view)).toBe('* hello');
  });

  it('strips indent before stripping bullet on indented bullet line', () => {
    const view = createView('  - hello', 4);
    markdownActionRegistry.outdent(view);
    expect(docText(view)).toBe('- hello');
  });

  it('does not strip top-level checkbox marker', () => {
    const view = createView('- [ ] task', 2);
    markdownActionRegistry.outdent(view);
    expect(docText(view)).toBe('- [ ] task');
  });

  it('strips all indent levels from bullet line, preserving marker', () => {
    const view = createView('\t\t- hello', 2);
    markdownActionRegistry.outdent(view);
    expect(docText(view)).toBe('\t- hello');
    markdownActionRegistry.outdent(view);
    expect(docText(view)).toBe('- hello');
    markdownActionRegistry.outdent(view);
    expect(docText(view)).toBe('- hello');
  });

  it('removes up to 4 spaces from indented bullet, preserving marker', () => {
    const view = createView('        - hello', 8);
    markdownActionRegistry.outdent(view);
    expect(docText(view)).toBe('    - hello');
    markdownActionRegistry.outdent(view);
    expect(docText(view)).toBe('- hello');
  });

  it('collapses extra spaces after dash bullet marker', () => {
    const view = createView('-    hello', 2);
    markdownActionRegistry.outdent(view);
    expect(docText(view)).toBe('- hello');
  });

  it('collapses extra spaces after asterisk bullet marker', () => {
    const view = createView('*    hello', 2);
    markdownActionRegistry.outdent(view);
    expect(docText(view)).toBe('* hello');
  });

  it('collapses extra spaces after checkbox marker', () => {
    const view = createView('- [ ]   task', 6);
    markdownActionRegistry.outdent(view);
    expect(docText(view)).toBe('- [ ] task');
  });

  it('collapses extra spaces after asterisk checkbox marker', () => {
    const view = createView('* [ ]   task', 6);
    markdownActionRegistry.outdent(view);
    expect(docText(view)).toBe('* [ ] task');
  });

  it('collapses extra spaces after numbered marker', () => {
    const view = createView('1.   hello', 4);
    markdownActionRegistry.outdent(view);
    expect(docText(view)).toBe('1. hello');
  });

  it('removes indent before normalizing spaces on bullet line', () => {
    const view = createView('\t-   hello', 2);
    markdownActionRegistry.outdent(view);
    expect(docText(view)).toBe('-   hello');
    markdownActionRegistry.outdent(view);
    expect(docText(view)).toBe('- hello');
  });

  it('no-op when marker already has exactly one space', () => {
    const view = createView('- hello', 2);
    const result = markdownActionRegistry.outdent(view);
    expect(result).toBe(true);
    expect(docText(view)).toBe('- hello');
  });
});

// ── insertCodeBlock ──────────────────────────────────────────────────────────

describe('codeBlock', () => {
  it('inserts fence pair at empty cursor', () => {
    const view = createView('', 0);
    markdownActionRegistry.codeBlock(view);
    expect(docText(view)).toBe('```\n\n```');
    expect(sel(view)).toEqual({ from: 4, to: 4 });
  });

  it('wraps selection in fences', () => {
    const view = createView('const x = 1;', 0, 12);
    markdownActionRegistry.codeBlock(view);
    expect(docText(view)).toBe('```\nconst x = 1;\n```');
    expect(sel(view)).toEqual({ from: 4, to: 16 });
  });

  it('unwraps when cursor on empty line between fences', () => {
    const view = createView('```\n\n```', 4);
    markdownActionRegistry.codeBlock(view);
    expect(docText(view)).toBe('');
  });

  it('unwraps selection inside existing fences', () => {
    const view = createView('```\ncode here\n```', 4, 13);
    markdownActionRegistry.codeBlock(view);
    expect(docText(view)).toBe('code here');
  });
});

// ── clearFormatting ──────────────────────────────────────────────────────────

describe('clearFormatting', () => {
  it('strips bold markers', () => {
    const view = createView('**hello**', 0, 9);
    markdownActionRegistry.clearFormatting(view);
    expect(docText(view)).toBe('hello');
  });

  it('strips italic markers', () => {
    const view = createView('*hello*', 0, 7);
    markdownActionRegistry.clearFormatting(view);
    expect(docText(view)).toBe('hello');
  });

  it('strips heading prefix', () => {
    const view = createView('## Hello', 0, 8);
    markdownActionRegistry.clearFormatting(view);
    expect(docText(view)).toBe('Hello');
  });

  it('strips mixed formatting', () => {
    const view = createView('## **hello** *world*', 0, 20);
    markdownActionRegistry.clearFormatting(view);
    expect(docText(view)).toBe('hello world');
  });

  it('operates on current line when no selection', () => {
    const view = createView('**hello**', 4);
    markdownActionRegistry.clearFormatting(view);
    expect(docText(view)).toBe('hello');
  });

  it('no-op when no formatting present', () => {
    const view = createView('plain text', 0, 10);
    markdownActionRegistry.clearFormatting(view);
    expect(docText(view)).toBe('plain text');
  });

  it('strips inline code markers', () => {
    const view = createView('`code`', 0, 6);
    markdownActionRegistry.clearFormatting(view);
    expect(docText(view)).toBe('code');
  });

  it('strips strikethrough markers', () => {
    const view = createView('~~hello~~', 0, 9);
    markdownActionRegistry.clearFormatting(view);
    expect(docText(view)).toBe('hello');
  });
});

// ── toggleQuote ──────────────────────────────────────────────────────────────

describe('quote (toggleQuote)', () => {
  it('adds quote prefix to unquoted line', () => {
    const view = createView('hello', 0);
    markdownActionRegistry.quote(view);
    expect(docText(view)).toBe('> hello');
  });

  it('removes quote prefix from quoted line', () => {
    const view = createView('> hello', 0);
    markdownActionRegistry.quote(view);
    expect(docText(view)).toBe('hello');
  });

  it('quotes all lines when selection has mixed quoting', () => {
    const view = createView('> line1\nline2', 0, 13);
    markdownActionRegistry.quote(view);
    expect(docText(view)).toBe('> > line1\n> line2');
  });

  it('unquotes all lines when all are quoted', () => {
    const view = createView('> line1\n> line2', 0, 14);
    markdownActionRegistry.quote(view);
    expect(docText(view)).toBe('line1\nline2');
  });
});

// ── createLink ───────────────────────────────────────────────────────────────

describe('createLink', () => {
  it('returns false on empty selection', () => {
    const view = createView('hello', 3);
    const result = markdownActionRegistry.createLink(view);
    expect(result).toBe(false);
    expect(docText(view)).toBe('hello');
  });

  it('wraps selection as markdown link', () => {
    const view = createView('hello world', 0, 5);
    markdownActionRegistry.createLink(view);
    expect(docText(view)).toBe('[hello]() world');
  });

  it('dispatches show-link-suggestions event', () => {
    const view = createView('hello', 0, 5);
    markdownActionRegistry.createLink(view);
    const event = emittedEvents.find((e) => e.type === 'show-link-suggestions');
    expect(event).toBeDefined();
    expect(event!.payload.editorView).toBe(view);
  });
});

// ── createNote ───────────────────────────────────────────────────────────────

describe('createNote', () => {
  beforeEach(() => {
    vi.stubGlobal('crypto', { randomUUID: () => 'test-uuid-1234' });
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('returns false on empty selection', () => {
    const view = createView('hello', 3);
    const result = markdownActionRegistry.createNote(view);
    expect(result).toBe(false);
  });

  // Panel quick preview (decision A): the focused editor renders the previewed
  // reference and notes are document-scoped there — refuse with a toast, no
  // note: link inserted, no create-note-from-editor emitted.
  it('focused reference + panel mode → false, toast, no link, no event', async () => {
    const { useAppStore } = await import('../../store/app-store');
    const { useUIStore } = await import('../../store/ui-store');
    const { claimFocus, unmountView } = await import('../../editor/active-editor');
    const showToast = vi.fn();
    useAppStore.setState({
      currentProject: { project_id: 'p1' } as any,
      currentDocument: { document_id: 'doc-1' } as any,
      currentReference: { reference_id: 'ref-1', document_id: 'doc-2' } as any,
      showToast,
    });
    useUIStore.getState().setRefOpenMode('doc-1', 'panel');
    const view = createView('hello world', 0, 5);
    claimFocus({ role: 'primary', isReference: true, view, handle: null });
    try {
      expect(markdownActionRegistry.createNote(view)).toBe(false);
      expect(docText(view)).toBe('hello world');
      expect(showToast).toHaveBeenCalledTimes(1);
      expect(showToast.mock.calls[0][1]).toBe('info');
      expect(emittedEvents.filter(e => e.type === 'create-note-from-editor')).toHaveLength(0);
    } finally {
      claimFocus({ role: 'primary', isReference: false, view, handle: null });
      unmountView(view);
      useUIStore.getState().setRefOpenMode('doc-1', 'center');
    }
  });
});

// ── openFind ─────────────────────────────────────────────────────────────────

describe('openFind', () => {
  it('emits open-find with the selected text and returns true', () => {
    // Selection anchor=0, head=5 → "hello".
    const view = createView('hello world', 0, 5);
    const result = markdownActionRegistry.openFind(view);
    expect(result).toBe(true);
    const event = emittedEvents.find((e) => e.type === 'open-find');
    expect(event).toBeDefined();
    expect(event!.payload).toEqual({ selection: 'hello' });
  });

  it('emits an empty selection when nothing is selected', () => {
    // Collapsed selection (anchor === head) → "".
    const view = createView('hello world', 3);
    const result = markdownActionRegistry.openFind(view);
    expect(result).toBe(true);
    const event = emittedEvents.find((e) => e.type === 'open-find');
    expect(event).toBeDefined();
    expect(event!.payload).toEqual({ selection: '' });
  });
});

// ── toggleBulletList ────────────────────────────────────────────────────────

describe('bulletList (toggleBulletList)', () => {
  it('adds bullet prefix to plain line', () => {
    const view = createView('hello', 0);
    markdownActionRegistry.bulletList(view);
    expect(docText(view)).toBe('- hello');
  });

  it('removes bullet prefix from bulleted line', () => {
    const view = createView('- hello', 2);
    markdownActionRegistry.bulletList(view);
    expect(docText(view)).toBe('hello');
  });

  it('removes checkbox prefix from checkbox line', () => {
    const view = createView('- [ ] task', 4);
    markdownActionRegistry.bulletList(view);
    expect(docText(view)).toBe('task');
  });

  it('removes checked checkbox prefix', () => {
    const view = createView('- [x] done', 4);
    markdownActionRegistry.bulletList(view);
    expect(docText(view)).toBe('done');
  });

  it('adds bullet to each line in multi-line selection', () => {
    const view = createView('line1\nline2\nline3', 0, 17);
    markdownActionRegistry.bulletList(view);
    expect(docText(view)).toBe('- line1\n- line2\n- line3');
  });

  it('removes bullet from all lines when all are bulleted', () => {
    const view = createView('- line1\n- line2', 0, 14);
    markdownActionRegistry.bulletList(view);
    expect(docText(view)).toBe('line1\nline2');
  });

  it('quotes all lines when selection has mixed bullet/plain', () => {
    const view = createView('- line1\nline2', 0, 12);
    markdownActionRegistry.bulletList(view);
    expect(docText(view)).toBe('- line1\n- line2');
  });

  it('converts checkbox to bullet when selection has mixed lines', () => {
    const view = createView('- [ ] task\nplain line', 0, 19);
    markdownActionRegistry.bulletList(view);
    expect(docText(view)).toBe('- task\n- plain line');
  });

  it('removes bullets from nested list', () => {
    const view = createView('- a\n\t- b\n\t\t- c', 0, 14);
    markdownActionRegistry.bulletList(view);
    expect(docText(view)).toBe('a\n\tb\n\t\tc');
  });

  it('adds bullets to nested plain lines', () => {
    const view = createView('a\n\tb', 0, 4);
    markdownActionRegistry.bulletList(view);
    expect(docText(view)).toBe('- a\n\t- b');
  });

  it('removes markers when all nested lines are checkboxes', () => {
    const view = createView('- [ ] a\n\t- [ ] b', 0, 16);
    markdownActionRegistry.bulletList(view);
    expect(docText(view)).toBe('a\n\tb');
  });

  it('places cursor at end of line after toggling bullet on single line', () => {
    const view = createView('hello', 0);
    markdownActionRegistry.bulletList(view);
    expect(docText(view)).toBe('- hello');
    expect(sel(view)).toEqual({ from: 7, to: 7 });
  });

  it('selects from startLine.from to endLine.to after multi-line bullet toggle', () => {
    const view = createView('line1\nline2', 0, 11);
    markdownActionRegistry.bulletList(view);
    expect(docText(view)).toBe('- line1\n- line2');
    expect(sel(view)).toEqual({ from: 0, to: 15 });
  });

  it('removes asterisk bullet prefix', () => {
    const view = createView('* hello', 2);
    markdownActionRegistry.bulletList(view);
    expect(docText(view)).toBe('hello');
  });

  it('removes mixed dash and asterisk bullets when all are bulleted', () => {
    const view = createView('- line1\n* line2', 0, 15);
    markdownActionRegistry.bulletList(view);
    expect(docText(view)).toBe('line1\nline2');
  });

  it('normalizes asterisk to dash when adding bullets to mixed selection', () => {
    const view = createView('* line1\nplain', 0, 13);
    markdownActionRegistry.bulletList(view);
    expect(docText(view)).toBe('- line1\n- plain');
  });

  it('skips empty lines when adding bullets to multi-line selection', () => {
    const view = createView('line1\n\nline2', 0, 12);
    markdownActionRegistry.bulletList(view);
    expect(docText(view)).toBe('- line1\n\n- line2');
  });

  it('strips bullets from non-empty lines only when toggling off with empty lines present', () => {
    const view = createView('- line1\n\n- line2', 0, 16);
    markdownActionRegistry.bulletList(view);
    expect(docText(view)).toBe('line1\n\nline2');
  });

  it('creates bullet on single empty line', () => {
    const view = createView('', 0);
    markdownActionRegistry.bulletList(view);
    expect(docText(view)).toBe('- ');
  });

  it('creates bullet on indented empty line', () => {
    const view = createView('\t', 1);
    markdownActionRegistry.bulletList(view);
    expect(docText(view)).toBe('\t- ');
  });

  it('does not create bullet on empty line in multi-line selection', () => {
    const view = createView('line1\n\nline2', 0, 12);
    markdownActionRegistry.bulletList(view);
    expect(docText(view)).toBe('- line1\n\n- line2');
  });
});

// ── toggleCheckboxList ──────────────────────────────────────────────────────

describe('checkboxList (toggleCheckboxList)', () => {
  it('adds unchecked checkbox to plain line', () => {
    const view = createView('task', 0);
    markdownActionRegistry.checkboxList(view);
    expect(docText(view)).toBe('- [ ] task');
  });

  it('converts regular bullet to unchecked checkbox', () => {
    const view = createView('- task', 2);
    markdownActionRegistry.checkboxList(view);
    expect(docText(view)).toBe('- [ ] task');
  });

  it('toggles unchecked checkbox to checked', () => {
    const view = createView('- [ ] task', 4);
    markdownActionRegistry.checkboxList(view);
    expect(docText(view)).toBe('- [x] task');
  });

  it('removes checked checkbox', () => {
    const view = createView('- [x] done', 4);
    markdownActionRegistry.checkboxList(view);
    expect(docText(view)).toBe('done');
  });

  it('toggles each line independently in multi-line selection', () => {
    const view = createView('plain\n- bullet\n- [ ] unch\n- [x] chk', 0, 31);
    markdownActionRegistry.checkboxList(view);
    expect(docText(view)).toBe('- [ ] plain\n- [ ] bullet\n- [x] unch\nchk');
  });

  it('creates checkbox on single empty line', () => {
    const view = createView('', 0);
    markdownActionRegistry.checkboxList(view);
    expect(docText(view)).toBe('- [ ] ');
  });

  it('does not create checkbox on empty line in multi-line selection', () => {
    const view = createView('task1\n\ntask2', 0, 12);
    markdownActionRegistry.checkboxList(view);
    expect(docText(view)).toBe('- [ ] task1\n\n- [ ] task2');
  });

  it('toggles nested checkboxes from unchecked to checked', () => {
    const view = createView('- [ ] a\n\t- [ ] b', 0, 16);
    markdownActionRegistry.checkboxList(view);
    expect(docText(view)).toBe('- [x] a\n\t- [x] b');
  });

  it('adds checkboxes to nested plain lines', () => {
    const view = createView('a\n\tb', 0, 4);
    markdownActionRegistry.checkboxList(view);
    expect(docText(view)).toBe('- [ ] a\n\t- [ ] b');
  });

  it('removes nested checked checkboxes', () => {
    const view = createView('- [x] a\n\t- [x] b', 0, 16);
    markdownActionRegistry.checkboxList(view);
    expect(docText(view)).toBe('a\n\tb');
  });

  it('places cursor at end of line after toggling checkbox on single line', () => {
    const view = createView('task', 0);
    markdownActionRegistry.checkboxList(view);
    expect(docText(view)).toBe('- [ ] task');
    expect(sel(view)).toEqual({ from: 10, to: 10 });
  });

  it('selects from startLine.from to endLine.to after multi-line checkbox toggle', () => {
    const view = createView('a\nb', 0, 3);
    markdownActionRegistry.checkboxList(view);
    expect(docText(view)).toBe('- [ ] a\n- [ ] b');
    expect(sel(view)).toEqual({ from: 0, to: 15 });
  });

  it('toggles nested mixed checkbox states independently', () => {
    const view = createView('- [ ] a\n\t- [x] b', 0, 16);
    markdownActionRegistry.checkboxList(view);
    expect(docText(view)).toBe('- [x] a\n\tb');
  });

  it('skips empty lines when adding checkboxes to multi-line selection', () => {
    const view = createView('task1\n\ntask2', 0, 12);
    markdownActionRegistry.checkboxList(view);
    expect(docText(view)).toBe('- [ ] task1\n\n- [ ] task2');
  });
});

// ── toggleNumberedList ──────────────────────────────────────────────────────

describe('numberedList (toggleNumberedList)', () => {
  it('adds numbered prefix to plain line', () => {
    const view = createView('hello', 0);
    markdownActionRegistry.numberedList(view);
    expect(docText(view)).toBe('1. hello');
  });

  it('removes numbered prefix from numbered line', () => {
    const view = createView('1. hello', 3);
    markdownActionRegistry.numberedList(view);
    expect(docText(view)).toBe('hello');
  });

  it('replaces bullet with numbered prefix', () => {
    const view = createView('- task', 2);
    markdownActionRegistry.numberedList(view);
    expect(docText(view)).toBe('1. task');
  });

  it('replaces checkbox with numbered prefix', () => {
    const view = createView('- [ ] task', 4);
    markdownActionRegistry.numberedList(view);
    expect(docText(view)).toBe('1. task');
  });

  it('numbers each line sequentially in multi-line selection', () => {
    const view = createView('line1\nline2\nline3', 0, 17);
    markdownActionRegistry.numberedList(view);
    expect(docText(view)).toBe('1. line1\n2. line2\n3. line3');
  });

  it('removes numbers from all lines when all are numbered', () => {
    const view = createView('1. a\n2. b\n3. c', 0, 14);
    markdownActionRegistry.numberedList(view);
    expect(docText(view)).toBe('a\nb\nc');
  });

  it('renumbers existing numbered list when mixed with plain lines', () => {
    const view = createView('1. a\nplain\n3. c', 0, 14);
    markdownActionRegistry.numberedList(view);
    expect(docText(view)).toBe('1. a\n2. plain\n3. c');
  });

  it('creates numbered marker on single empty line', () => {
    const view = createView('', 0);
    markdownActionRegistry.numberedList(view);
    expect(docText(view)).toBe('1. ');
  });

  it('does not create numbered marker on empty line in multi-line selection', () => {
    const view = createView('line1\n\nline2\nline3', 0, 18);
    markdownActionRegistry.numberedList(view);
    expect(docText(view)).toBe('1. line1\n\n2. line2\n3. line3');
  });

  it('removes numbers from nested numbered list', () => {
    const view = createView('1. a\n\t2. b', 0, 10);
    markdownActionRegistry.numberedList(view);
    expect(docText(view)).toBe('a\n\tb');
  });

  it('adds numbers to nested plain lines', () => {
    const view = createView('a\n\tb', 0, 4);
    markdownActionRegistry.numberedList(view);
    expect(docText(view)).toBe('1. a\n\t2. b');
  });

  it('places cursor at end of line after toggling numbered on single line', () => {
    const view = createView('hello', 0);
    markdownActionRegistry.numberedList(view);
    expect(docText(view)).toBe('1. hello');
    expect(sel(view)).toEqual({ from: 8, to: 8 });
  });

  it('selects from startLine.from to endLine.to after multi-line numbered toggle', () => {
    const view = createView('line1\nline2', 0, 11);
    markdownActionRegistry.numberedList(view);
    expect(docText(view)).toBe('1. line1\n2. line2');
    expect(sel(view)).toEqual({ from: 0, to: 17 });
  });

  it('skips empty lines when numbering multi-line selection', () => {
    const view = createView('line1\n\nline2\nline3', 0, 18);
    markdownActionRegistry.numberedList(view);
    expect(docText(view)).toBe('1. line1\n\n2. line2\n3. line3');
  });
});
