/**
 * Unit tests for list-nav — Cmd+Left/Right (paragraphs and lists) and ArrowDown/Up/Left.
 *
 * Note: jsdom does not perform layout, so `view.moveToLineBoundary` collapses
 * visual line boundaries onto logical line boundaries. Wrapped-line behavior
 * (visEnd ≠ lineTo, visStart ≠ lineFrom) is verified manually in a real browser.
 */
// @vitest-environment jsdom
import { describe, it, expect } from 'vitest';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { markdown, markdownLanguage } from '@codemirror/lang-markdown';
import { ensureSyntaxTree } from '@codemirror/language';
import {
  cursorLineStartStep,
  cursorLineEndStep,
  selectLineStartStep,
  selectLineEndStep,
  arrowDownIntoList,
  arrowUpIntoList,
  arrowLeftAtListContentStart,
} from './list-nav';

function createView(doc: string, cursor: number): EditorView {
  const state = EditorState.create({
    doc,
    selection: { anchor: cursor },
    extensions: [markdown({ base: markdownLanguage })],
  });
  const view = new EditorView({ state, parent: document.createElement('div') });
  ensureSyntaxTree(view.state, view.state.doc.length, 5000);
  view.dispatch({ selection: { anchor: cursor } });
  return view;
}

function createViewWithSelection(doc: string, anchor: number, head: number): EditorView {
  const state = EditorState.create({
    doc,
    selection: { anchor, head },
    extensions: [markdown({ base: markdownLanguage })],
  });
  const view = new EditorView({ state, parent: document.createElement('div') });
  ensureSyntaxTree(view.state, view.state.doc.length, 5000);
  view.dispatch({ selection: { anchor, head } });
  return view;
}

describe('cursorLineStartStep — Cmd+ArrowLeft (lists, multi-step)', () => {

  it('third press at lineFrom returns false', () => {
    const doc = '- bullet text';
    const view = createView(doc, doc.length);
    cursorLineStartStep(view);
    cursorLineStartStep(view);
    expect(cursorLineStartStep(view)).toBe(false);
  });

  it('returns false when already at lineFrom', () => {
    const doc = '- bullet text';
    const view = createView(doc, 0);
    expect(cursorLineStartStep(view)).toBe(false);
  });

  it('cursor between lineFrom and contentStart goes to lineFrom', () => {
    const doc = '- bullet text';
    const view = createView(doc, 1);
    expect(cursorLineStartStep(view)).toBe(true);
    expect(view.state.selection.main.head).toBe(0);
  });
});

describe('cursorLineStartStep — Cmd+ArrowLeft (headings, multi-step)', () => {

  it('third press at lineFrom returns false', () => {
    const doc = '# heading text';
    const view = createView(doc, doc.length);
    cursorLineStartStep(view);
    cursorLineStartStep(view);
    expect(cursorLineStartStep(view)).toBe(false);
  });

  it('returns false when already at lineFrom', () => {
    const doc = '# heading text';
    const view = createView(doc, 0);
    expect(cursorLineStartStep(view)).toBe(false);
  });

  it('cursor between lineFrom and contentStart goes to lineFrom', () => {
    const doc = '# heading text';
    const view = createView(doc, 1);
    expect(cursorLineStartStep(view)).toBe(true);
    expect(view.state.selection.main.head).toBe(0);
  });
});

describe('selectLineStartStep — Cmd+Shift+ArrowLeft (headings)', () => {

  it('second press extends selection to lineFrom', () => {
    const doc = '# heading text';
    const view = createView(doc, doc.length);
    selectLineStartStep(view);
    selectLineStartStep(view);
    expect(view.state.selection.main.head).toBe(0);
    expect(view.state.selection.main.anchor).toBe(doc.length);
  });
});

describe('cursorLineEndStep — Cmd+ArrowRight (paragraphs and lists)', () => {

  it('works for nested bullet', () => {
    const doc = '  - nested bullet';
    const view = createView(doc, 4);
    expect(cursorLineEndStep(view)).toBe(true);
    expect(view.state.selection.main.head).toBe(doc.length);
  });

  it('works for numbered list', () => {
    const doc = '1. numbered text';
    const view = createView(doc, 3);
    expect(cursorLineEndStep(view)).toBe(true);
    expect(view.state.selection.main.head).toBe(doc.length);
  });
});

describe('selectLineStartStep — Cmd+Shift+ArrowLeft', () => {

  it('second press extends selection to lineFrom', () => {
    const doc = '- bullet text';
    const view = createView(doc, doc.length);
    selectLineStartStep(view);
    selectLineStartStep(view);
    expect(view.state.selection.main.head).toBe(0);
    expect(view.state.selection.main.anchor).toBe(doc.length);
  });

  it('preserves existing anchor when extending', () => {
    const doc = '- bullet text';
    const anchor = 5;
    const view = createViewWithSelection(doc, anchor, doc.length);
    selectLineStartStep(view);
    expect(view.state.selection.main.anchor).toBe(anchor);
    expect(view.state.selection.main.head).toBe(2);
  });

  it('works with numbered list', () => {
    const doc = '1. numbered text';
    const view = createView(doc, doc.length);
    selectLineStartStep(view);
    expect(view.state.selection.main.head).toBe(3);
  });
});

describe('selectLineEndStep — Cmd+Shift+ArrowRight', () => {

  it('preserves existing anchor when extending', () => {
    const doc = '- bullet text';
    const anchor = 2;
    const view = createViewWithSelection(doc, anchor, 5);
    selectLineEndStep(view);
    expect(view.state.selection.main.anchor).toBe(anchor);
    expect(view.state.selection.main.head).toBe(doc.length);
  });

  it('works for numbered list', () => {
    const doc = '1. numbered text';
    const view = createView(doc, 3);
    expect(selectLineEndStep(view)).toBe(true);
    expect(view.state.selection.main.head).toBe(doc.length);
  });
});

describe('arrowDownIntoList — vertical snap on ArrowDown', () => {
  it('snaps cursor to content start when landing on bullet from above (col 0)', () => {
    const doc = 'line above\n- bullet text';
    const view = createView(doc, 0);
    const result = arrowDownIntoList(view);
    expect(result).toBe(true);
    expect(view.state.selection.main.head).toBe(11 + 2);
  });

  it('returns false when next line is not a list item', () => {
    const doc = 'first line\nsecond line';
    const view = createView(doc, 0);
    expect(arrowDownIntoList(view)).toBe(false);
  });

  it('returns false when target col is already past content start', () => {
    const doc = 'line above\n- bullet text';
    const view = createView(doc, 5);
    expect(arrowDownIntoList(view)).toBe(false);
  });

  it('returns false on last line', () => {
    const doc = '- bullet text';
    const view = createView(doc, doc.length);
    expect(arrowDownIntoList(view)).toBe(false);
  });

  it('snaps to content start for nested bullet (indented)', () => {
    const doc = 'line above\n  - nested bullet';
    const view = createView(doc, 0);
    const result = arrowDownIntoList(view);
    expect(result).toBe(true);
    expect(view.state.selection.main.head).toBe(11 + 4);
  });

  it('snaps to content start for numbered list', () => {
    const doc = 'line above\n1. numbered text';
    const view = createView(doc, 0);
    const result = arrowDownIntoList(view);
    expect(result).toBe(true);
    expect(view.state.selection.main.head).toBe(11 + 3);
  });

  it('does not interfere when moving between non-list lines', () => {
    const doc = 'aaa\nbbb\nccc';
    const view = createView(doc, 2);
    expect(arrowDownIntoList(view)).toBe(false);
  });
});

describe('arrowUpIntoList — vertical snap on ArrowUp', () => {
  it('snaps cursor to content start when landing on bullet from below (col 0)', () => {
    const doc = '- bullet text\nline below';
    const belowStart = doc.indexOf('\n') + 1;
    const view = createView(doc, belowStart);
    const result = arrowUpIntoList(view);
    expect(result).toBe(true);
    expect(view.state.selection.main.head).toBe(2);
  });

  it('returns false when previous line is not a list item', () => {
    const doc = 'first line\nsecond line';
    const view = createView(doc, doc.length);
    expect(arrowUpIntoList(view)).toBe(false);
  });

  it('returns false when target col is already past content start', () => {
    const doc = '- bullet text\nline below';
    const col = 5;
    const belowCursor = doc.indexOf('\n') + 1 + col;
    const view2 = createView(doc, belowCursor);
    expect(arrowUpIntoList(view2)).toBe(false);
  });

  it('returns false on first line', () => {
    const view2 = createView('- bullet text', 5);
    expect(arrowUpIntoList(view2)).toBe(false);
  });

  it('snaps to content start for nested bullet (indented)', () => {
    const doc = '  - nested bullet\nline below';
    const belowStart = doc.indexOf('\n') + 1;
    const view = createView(doc, belowStart);
    const result = arrowUpIntoList(view);
    expect(result).toBe(true);
    expect(view.state.selection.main.head).toBe(4);
  });

  it('bidirectional consistency: ArrowDown and ArrowUp land at same position', () => {
    const doc = 'aaa\n- bullet text\nccc';
    const contentStart = doc.indexOf('- bullet text') + 2;
    const viewDown = createView(doc, 1);
    const downResult = arrowDownIntoList(viewDown);
    expect(downResult).toBe(true);
    expect(viewDown.state.selection.main.head).toBe(contentStart);

    const cccStart = doc.lastIndexOf('\n') + 1;
    const viewUp = createView(doc, cccStart);
    const upResult = arrowUpIntoList(viewUp);
    expect(upResult).toBe(true);
    expect(viewUp.state.selection.main.head).toBe(contentStart);

    expect(viewDown.state.selection.main.head).toBe(viewUp.state.selection.main.head);
  });
});

describe('arrowLeftAtListContentStart — gateway into marker zone', () => {
  it('moves cursor from content start to line start', () => {
    const doc = '- bullet text';
    const view = createView(doc, 2);
    const result = arrowLeftAtListContentStart(view);
    expect(result).toBe(true);
    expect(view.state.selection.main.head).toBe(0);
  });

  it('returns false when cursor is not at content start', () => {
    const doc = '- bullet text';
    const view = createView(doc, 5);
    expect(arrowLeftAtListContentStart(view)).toBe(false);
  });

  it('returns false for non-list lines', () => {
    const doc = 'just regular text';
    const view = createView(doc, 0);
    expect(arrowLeftAtListContentStart(view)).toBe(false);
  });

  it('returns false when cursor is already at line start', () => {
    const doc = '- bullet text';
    const view = createView(doc, 0);
    expect(arrowLeftAtListContentStart(view)).toBe(false);
  });

  it('works for nested bullet', () => {
    const doc = '  - nested bullet';
    const view = createView(doc, 4);
    const result = arrowLeftAtListContentStart(view);
    expect(result).toBe(true);
    expect(view.state.selection.main.head).toBe(0);
  });

  it('works for numbered list', () => {
    const doc = '1. numbered text';
    const view = createView(doc, 3);
    const result = arrowLeftAtListContentStart(view);
    expect(result).toBe(true);
    expect(view.state.selection.main.head).toBe(0);
  });
});

describe('Cmd+Arrow delimiter stops — sentence-ending punctuation', () => {
  // 'one. two! three?' — '.'@3, '!'@8, '?'@15, length 17
  it('right: stops after ., after !, after ?, then lineTo', () => {
    const doc = 'one. two! three?';
    const view = createView(doc, 0);
    cursorLineEndStep(view);
    expect(view.state.selection.main.head).toBe(doc.indexOf('.') + 1);
    cursorLineEndStep(view);
    expect(view.state.selection.main.head).toBe(doc.indexOf('!') + 1);
    cursorLineEndStep(view);
    expect(view.state.selection.main.head).toBe(doc.indexOf('?') + 1);
    cursorLineEndStep(view);
    expect(view.state.selection.main.head).toBe(doc.length);
    expect(cursorLineEndStep(view)).toBe(false);
  });

  it('left: stops before ?, before !, before ., then lineFrom', () => {
    const doc = 'one. two! three?';
    const view = createView(doc, doc.length);
    cursorLineStartStep(view);
    expect(view.state.selection.main.head).toBe(doc.indexOf('?'));
    cursorLineStartStep(view);
    expect(view.state.selection.main.head).toBe(doc.indexOf('!'));
    cursorLineStartStep(view);
    expect(view.state.selection.main.head).toBe(doc.indexOf('.'));
    cursorLineStartStep(view);
    expect(view.state.selection.main.head).toBe(0);
    expect(cursorLineStartStep(view)).toBe(false);
  });
});
