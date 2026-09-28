/** Unit tests for clarifyBlock — quote → blockquote + question serialization. */

import { describe, it, expect } from 'vitest';
import { clarifyBlock } from './clarify-format';

describe('clarifyBlock', () => {
  it('quotes a single-line fragment and appends the question', () => {
    expect(clarifyBlock('the answer', 'why?')).toBe('> the answer\n\nwhy?');
  });

  it('prefixes every line of a multi-line fragment with "> "', () => {
    expect(clarifyBlock('line one\nline two', 'what about this?')).toBe(
      '> line one\n> line two\n\nwhat about this?',
    );
  });

  it('keeps blank lines inside the fragment as bare "> " quote lines', () => {
    expect(clarifyBlock('a\n\nb', 'q')).toBe('> a\n> \n> b\n\nq');
  });

  it('separates the question from the quote by a blank line so it is its own paragraph', () => {
    const [quoteBlock, ...rest] = clarifyBlock('the answer', 'why?').split('\n\n');
    expect(quoteBlock).toBe('> the answer');
    expect(rest).toEqual(['why?']);
  });
});
