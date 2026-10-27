import { describe, it, expect } from 'vitest';
import { insertAtCaret } from './insert-at-caret';

function textarea(value: string, start: number, end = start): HTMLTextAreaElement {
  const el = document.createElement('textarea');
  el.value = value;
  el.setSelectionRange(start, end);
  return el;
}

describe('insertAtCaret', () => {
  it('inserts at the caret in the middle, padding both sides', () => {
    const value = 'hello world';
    expect(insertAtCaret(value, 'big', textarea(value, 5))).toEqual({ text: 'hello big world', caret: 9 });
  });

  it('inserts at the start without a leading space', () => {
    const value = 'world';
    expect(insertAtCaret(value, 'hello', textarea(value, 0))).toEqual({ text: 'hello world', caret: 5 });
  });

  it('does not double existing whitespace', () => {
    const value = 'a  b';
    expect(insertAtCaret(value, 'x', textarea(value, 2))).toEqual({ text: 'a x b', caret: 3 });
  });

  it('replaces the selection', () => {
    const value = 'one two three';
    expect(insertAtCaret(value, '2', textarea(value, 4, 7))).toEqual({ text: 'one 2 three', caret: 5 });
  });

  it('appends when there is no textarea', () => {
    expect(insertAtCaret('draft', 'more', null)).toEqual({ text: 'draft more', caret: 10 });
    expect(insertAtCaret('', 'first', null)).toEqual({ text: 'first', caret: 5 });
  });
});
