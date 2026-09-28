/** Unit tests for headingsEqual — the anti-churn diff that gates store writes
 * in live-headings-extension. A false "equal" silently drops a real TOC update;
 * a false "unequal" re-renders the TOC on every keystroke. */
import { describe, it, expect } from 'vitest';
import { headingsEqual } from './live-headings-extension';
import type { HeadingItem } from '../types';

const h = (level: number, text: string, line: number): HeadingItem => ({ level, text, line });

describe('headingsEqual', () => {
  it('is true for identical lists', () => {
    expect(headingsEqual([h(1, 'A', 1), h(2, 'B', 3)], [h(1, 'A', 1), h(2, 'B', 3)])).toBe(true);
  });

  it('is true for two empty lists', () => {
    expect(headingsEqual([], [])).toBe(true);
  });

  it('is false when length differs', () => {
    expect(headingsEqual([h(1, 'A', 1)], [h(1, 'A', 1), h(2, 'B', 2)])).toBe(false);
  });

  it('is false when text differs (heading edited)', () => {
    expect(headingsEqual([h(1, 'A', 1)], [h(1, 'A!', 1)])).toBe(false);
  });

  it('is false when line shifts (heading moved by edits above)', () => {
    expect(headingsEqual([h(1, 'A', 1)], [h(1, 'A', 2)])).toBe(false);
  });

  it('is false when level changes (# → ##)', () => {
    expect(headingsEqual([h(1, 'A', 1)], [h(2, 'A', 1)])).toBe(false);
  });
});
