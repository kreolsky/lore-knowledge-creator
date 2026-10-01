/** Plan "chat-message-queue": the pure coalescing function.

 * A burst of afterthought chips becomes ONE follow-up user message: parts joined
 * with a blank line between them, no bullets (the user wrote prose). Blank/whitespace-
 * only chips are dropped so an accidental empty enqueue never produces a stray line. */

import { describe, it, expect } from 'vitest';
import { joinQueued } from './queue-join';

describe('joinQueued', () => {
  it('empty array → empty string', () => {
    expect(joinQueued([])).toBe('');
  });
  it('single part → that part trimmed', () => {
    expect(joinQueued(['  hi  '])).toBe('hi');
  });
  it('two parts → blank line between them', () => {
    expect(joinQueued(['a', 'b'])).toBe('a\n\nb');
  });
  it('three parts → two blank-line separators', () => {
    expect(joinQueued(['one', 'two', 'three'])).toBe('one\n\ntwo\n\nthree');
  });
  it('drops whitespace-only parts but keeps the rest in order', () => {
    expect(joinQueued(['a', '   ', 'b'])).toBe('a\n\nb');
  });
  it('all-blank parts → empty string', () => {
    expect(joinQueued(['   ', '', '\t'])).toBe('');
  });
  it('preserves internal whitespace within a part', () => {
    expect(joinQueued(['line one\nline two', 'next'])).toBe('line one\nline two\n\nnext');
  });
});
