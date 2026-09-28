/** Unit tests for cursor-preserve helpers. */
import { describe, it, expect } from 'vitest';
import { EditorState, EditorSelection } from '@codemirror/state';
import { preserveCursorFromLineEnd, snapSelectionToLineEnd } from './cursor-preserve';

function makeState(doc: string): EditorState {
  return EditorState.create({ doc });
}

describe('preserveCursorFromLineEnd', () => {
  it('keeps end-of-line cursor at end of line after a leading insert', () => {
    const prev = makeState('- [ ] ');
    const sel = EditorSelection.cursor(6);
    const changes = [{ from: 0, insert: '\t' }];
    const next = preserveCursorFromLineEnd(prev, changes, sel);
    expect(next.anchor).toBe(7);
    expect(next.head).toBe(7);
  });

  it('preserves mid-line offset from line end across an insert', () => {
    // `- [ ] foo` len=9, cursor at 8 (between `fo` and `o`). Offset from line.to: 1.
    const prev = makeState('- [ ] foo');
    const sel = EditorSelection.cursor(8);
    const changes = [{ from: 0, insert: '\t' }];
    const next = preserveCursorFromLineEnd(prev, changes, sel);
    expect(next.anchor).toBe(9);
  });

  it('preserves cursor relative to line end across an outdent', () => {
    // `\t- foo` len=6, cursor at 4 (between ` ` and `f`). offset = 6-4 = 2.
    // After removing leading tab: `- foo` len=5. New cursor at 5-2 = 3.
    const prev = makeState('\t- foo');
    const sel = EditorSelection.cursor(4);
    const changes = [{ from: 0, to: 1 }];
    const next = preserveCursorFromLineEnd(prev, changes, sel);
    expect(next.anchor).toBe(3);
  });

  it('clamps to new line.from when offset exceeds new line length', () => {
    // `\thello` len=6, cursor at 6. offset = 0. After outdent: `hello` len=5.
    const prev = makeState('\thello');
    const sel = EditorSelection.cursor(6);
    const changes = [{ from: 0, to: 1 }];
    const next = preserveCursorFromLineEnd(prev, changes, sel);
    expect(next.anchor).toBe(5);
  });

  it('preserves both endpoints of a multi-line range independently', () => {
    // 3 lines: `aaa\nbbb\nccc`. Selection from line1.to=3 to line3.to=11.
    const prev = makeState('aaa\nbbb\nccc');
    const sel = EditorSelection.range(3, 11);
    const changes = [
      { from: 0, insert: '\t' },
      { from: 4, insert: '\t' },
      { from: 8, insert: '\t' },
    ];
    const next = preserveCursorFromLineEnd(prev, changes, sel);
    // anchor (line 1 end) was at 3, line.to became 4 → 4
    // head (line 3 end) was at 11, line.to in new doc became 14 → 14
    expect(next.anchor).toBe(4);
    expect(next.head).toBe(14);
  });
});

describe('snapSelectionToLineEnd', () => {
  it('places empty-selection cursor at end of post-change endLine', () => {
    const prev = makeState('hello');
    const changes = [{ from: 0, insert: '- ' }];
    const next = snapSelectionToLineEnd(prev, changes, 1, 1, true);
    expect(next.anchor).toBe(7);
    expect(next.head).toBe(7);
  });

  it('selects from new startLine.from to endLine.to for multi-line range', () => {
    const prev = makeState('line1\nline2');
    const changes = [
      { from: 0, insert: '- ' },
      { from: 6, insert: '- ' },
    ];
    const next = snapSelectionToLineEnd(prev, changes, 1, 2, false);
    expect(next.anchor).toBe(0);
    expect(next.head).toBe(15);
  });

  it('handles a no-op change set (no inserts)', () => {
    const prev = makeState('aaa');
    const next = snapSelectionToLineEnd(prev, [], 1, 1, true);
    expect(next.anchor).toBe(3);
  });
});
