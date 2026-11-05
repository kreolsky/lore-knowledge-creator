import { describe, expect, it } from 'vitest';
import { clipboardImages } from './file-drop-handler';

/**
 * REGRESSION(#1598/#1599): a synthetic paste event (jsdom, embedded webviews)
 * carries a text-only clipboardData with NO `files` list. That payload is
 * "no images", never a TypeError — the capture-phase listener must not throw.
 */
describe('clipboardImages', () => {
  // TEST-WHY: data-loss — a clipboard payload with text but no files list is a text paste (the text-wins rule); it must yield no images, not a TypeError in the capture-phase listener.
  it('a text-only clipboard payload without a files list yields no images', () => {
    const data = {
      getData: () => '| A | B |\n| --- | --- |\n| 1 | 2 |',
    } as unknown as DataTransfer;
    expect(clipboardImages(data)).toEqual([]);
  });
});
