/** Unit tests for list-continuation — Enter key behavior in bullet/numbered lists. */

// @vitest-environment jsdom
import { describe, it, expect } from 'vitest';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { markdown, markdownLanguage } from '@codemirror/lang-markdown';
import { indentUnit } from '@codemirror/language';
import { ensureSyntaxTree } from '@codemirror/language';
import { continueListWithSpace } from './list-continuation';

const continueList = continueListWithSpace;

function createView(doc: string, cursor: number): EditorView {
  const state = EditorState.create({
    doc,
    selection: { anchor: cursor },
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

function runEnter(view: EditorView): boolean {
  // Pass view directly: keymap commands receive an EditorView whose `state`
  // is a live getter. Wrapping in a plain `{state, dispatch}` snapshot causes
  // post-dispatch reads to see stale state.
  return continueList(view);
}

function docText(view: EditorView): string {
  return view.state.doc.toString();
}

describe('list continuation — Enter on non-empty bullets', () => {
  it('continues level 1 bullet with same marker (-)', () => {
    const view = createView('- hello', 7);
    const handled = runEnter(view);
    expect(handled).toBe(true);
    expect(docText(view)).toBe('- hello\n- ');
  });

  it('continues level 1 bullet with asterisk marker', () => {
    const view = createView('* hello', 7);
    const handled = runEnter(view);
    expect(handled).toBe(true);
    expect(docText(view)).toBe('* hello\n* ');
  });

  it('continues level 1 bullet with plus marker', () => {
    const view = createView('+ hello', 7);
    const handled = runEnter(view);
    expect(handled).toBe(true);
    expect(docText(view)).toBe('+ hello\n+ ');
  });

  it('continues level 2 nested bullet at same indent', () => {
    const view = createView('- one\n\t- two', 12);
    const handled = runEnter(view);
    expect(handled).toBe(true);
    expect(docText(view)).toBe('- one\n\t- two\n\t- ');
  });

  it('continues level 3 nested bullet at same indent', () => {
    const doc = '- one\n\t- two\n\t\t- three';
    const view = createView(doc, doc.length);
    const handled = runEnter(view);
    expect(handled).toBe(true);
    expect(docText(view)).toBe('- one\n\t- two\n\t\t- three\n\t\t- ');
  });
});

describe('list continuation — Enter on empty bullets (dedent)', () => {
  it('empty level 2 bullet dedents to level 1', () => {
    const view = createView('- one\n\t- ', 9);
    const handled = runEnter(view);
    expect(handled).toBe(true);
    expect(docText(view)).toBe('- one\n- ');
  });

  it('empty level 3 bullet dedents to level 2', () => {
    const doc = '- one\n\t- two\n\t\t- ';
    const view = createView(doc, doc.length);
    const handled = runEnter(view);
    expect(handled).toBe(true);
    expect(docText(view)).toBe('- one\n\t- two\n\t- ');
  });

  it('empty level 1 bullet removes marker entirely', () => {
    const view = createView('- ', 2);
    const handled = runEnter(view);
    expect(handled).toBe(true);
    expect(docText(view)).toBe('');
  });
});

describe('list continuation — checkbox trailing space', () => {
  it('continues checkbox with trailing space after marker', () => {
    const view = createView('- [ ] foo', 9);
    const handled = runEnter(view);
    expect(handled).toBe(true);
    expect(docText(view)).toBe('- [ ] foo\n- [ ] ');
    expect(view.state.selection.main.head).toBe(view.state.doc.length);
  });

  it('continues checked checkbox with unchecked + trailing space', () => {
    const view = createView('- [x] done', 10);
    const handled = runEnter(view);
    expect(handled).toBe(true);
    expect(docText(view).endsWith('- [ ] ') || docText(view).endsWith('- [x] ')).toBe(true);
  });

  it('does not double-space if continuation already has trailing space', () => {
    const view = createView('- [ ] foo', 9);
    const handled = runEnter(view);
    expect(handled).toBe(true);
    expect(docText(view)).not.toMatch(/  $/);
  });

  it('empty level-1 checkbox dedents to empty line (existing behavior)', () => {
    const view = createView('- [ ] ', 6);
    const handled = runEnter(view);
    expect(handled).toBe(true);
    expect(docText(view)).toBe('');
  });

  it('continues level-2 checkbox with trailing space', () => {
    const view = createView('- [ ] one\n\t- [ ] two', 20);
    const handled = runEnter(view);
    expect(handled).toBe(true);
    expect(docText(view)).toBe('- [ ] one\n\t- [ ] two\n\t- [ ] ');
    expect(view.state.selection.main.head).toBe(view.state.doc.length);
  });

  it('exact user scenario: aaa, Enter, Tab, bbb, Enter', () => {
    // Step 1: '- [ ] aaa', cursor at end.
    let view = createView('- [ ] aaa', 9);
    expect(runEnter(view)).toBe(true);
    expect(docText(view)).toBe('- [ ] aaa\n- [ ] ');
    // Step 2: Tab simulated as inserting \t at line.from.
    const cursorAfterEnter = view.state.selection.main.head;
    const line2 = view.state.doc.lineAt(cursorAfterEnter);
    view.dispatch({
      changes: { from: line2.from, insert: '\t' },
      selection: { anchor: cursorAfterEnter + 1 },
    });
    expect(docText(view)).toBe('- [ ] aaa\n\t- [ ] ');
    // Step 3: Type 'bbb'.
    const pos = view.state.selection.main.head;
    view.dispatch({ changes: { from: pos, insert: 'bbb' }, selection: { anchor: pos + 3 } });
    expect(docText(view)).toBe('- [ ] aaa\n\t- [ ] bbb');
    // Step 4: Enter — should continue at level 2 with trailing space.
    expect(runEnter(view)).toBe(true);
    expect(docText(view)).toBe('- [ ] aaa\n\t- [ ] bbb\n\t- [ ] ');
    expect(view.state.selection.main.head).toBe(view.state.doc.length);
  });
});

describe('list continuation — outside list context', () => {
  it('returns false for plain text', () => {
    const view = createView('hello world', 5);
    const handled = runEnter(view);
    expect(handled).toBe(false);
  });

  it('returns false for heading', () => {
    const view = createView('# Title', 7);
    const handled = runEnter(view);
    expect(handled).toBe(false);
  });
});
