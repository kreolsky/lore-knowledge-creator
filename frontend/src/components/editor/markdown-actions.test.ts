/** Unit tests for markdown formatting actions — CM6 EditorView in jsdom. */

// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
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

// ── createLink ───────────────────────────────────────────────────────────────

describe('createLink', () => {
  it('returns false on empty selection', () => {
    const view = createView('hello', 3);
    const result = markdownActionRegistry.createLink(view);
    expect(result).toBe(false);
    expect(docText(view)).toBe('hello');
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
});
