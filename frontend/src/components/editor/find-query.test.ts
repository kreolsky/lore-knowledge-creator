/** Unit tests for the pure SearchQuery builder used by FindReplacePanel. */

import { describe, it, expect } from 'vitest';
import { buildSearchQuery } from './find-query';

describe('buildSearchQuery', () => {
  it('maps all option flags and strings onto the SearchQuery', () => {
    const q = buildSearchQuery({
      search: 'foo',
      replace: 'bar',
      caseSensitive: true,
      regexp: true,
      wholeWord: true,
    });
    expect(q.search).toBe('foo');
    expect(q.replace).toBe('bar');
    expect(q.caseSensitive).toBe(true);
    expect(q.regexp).toBe(true);
    expect(q.wholeWord).toBe(true);
  });

  it('defaults flags to false', () => {
    const q = buildSearchQuery({
      search: 'x',
      replace: '',
      caseSensitive: false,
      regexp: false,
      wholeWord: false,
    });
    expect(q.caseSensitive).toBe(false);
    expect(q.regexp).toBe(false);
    expect(q.wholeWord).toBe(false);
    expect(q.replace).toBe('');
  });
});
