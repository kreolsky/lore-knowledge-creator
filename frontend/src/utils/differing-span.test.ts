/** differingSpan / mapRangeThrough: the public page's in-place text refresh and the draft selection it carries. */
import { describe, it, expect } from 'vitest';
import { differingSpan, mapRangeThrough } from './differing-span';

const TEXT = 'Начало 😀 ключевые слова — конец';
const START = TEXT.indexOf('ключевые');
const END = START + 'ключевые слова'.length;

function carried(after: string): string | null {
  const range = mapRangeThrough(differingSpan(TEXT, after)!, START, END);
  return range && after.slice(range.start, range.end);
}

describe('mapRangeThrough', () => {
  it('follows the words across an edit before or after them', () => {
    expect(carried('Ж😀 вставка ' + TEXT)).toBe('ключевые слова');
    expect(carried(TEXT + ' и хвост 😀')).toBe('ключевые слова');
  });

  it('does not grow when text is typed exactly at either edge', () => {
    expect(carried(TEXT.slice(0, START) + 'ёё' + TEXT.slice(START))).toBe('ключевые слова');
    expect(carried(TEXT.slice(0, END) + 'ёё' + TEXT.slice(END))).toBe('ключевые слова');
  });

  it('keeps the surviving part when the edit eats into the range, and is null when it is all gone', () => {
    expect(carried(TEXT.replace('ключевые ', ''))).toBe('слова');
    expect(carried(TEXT.replace('ключевые слова', 'другое'))).toBeNull();
  });
});
