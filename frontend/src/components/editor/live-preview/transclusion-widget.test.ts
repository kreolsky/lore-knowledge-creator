/** Tests for TransclusionWidget — toDOM structure and header navigation. */

// @vitest-environment jsdom
import { describe, it, expect, beforeEach } from 'vitest';
import { TransclusionWidget, widgetHeightCache } from './widgets';
import { transcludeMap } from './effects';

describe('TransclusionWidget', () => {
  beforeEach(() => {
    transcludeMap.clear();
    widgetHeightCache.clear();
  });

  it('title sits inside a full-width header row (text-only hover/click target)', () => {
    // The divider lives on the header (full row); the title is inline-block so its
    // hover/click target is the title text, not the empty space beside it.
    const w = new TransclusionWidget(
      { kind: 'doc', title: 'T', content: 'c' },
      'd',
      'doc',
    );
    const el = w.toDOM();
    const title = el.querySelector('.cm-transclusion-title') as HTMLElement;
    const header = title.parentElement;
    expect(header?.className).toBe('cm-transclusion-header');
    expect(header?.parentElement).toBe(el);
  });

  it('strips a single trailing EOF newline so the embed has no phantom last line', () => {
    const w = new TransclusionWidget({ kind: 'doc', title: 'D', content: 'line one\n' }, 'd-nl', 'doc', 0);
    w.toDOM();
    expect(w.nestedView?.state.doc.toString()).toBe('line one');
  });

  it('preserves intentional blank lines (only the EOF newline is stripped)', () => {
    const w = new TransclusionWidget({ kind: 'doc', title: 'D', content: 'a\n\nb\n' }, 'd-bl', 'doc', 0);
    w.toDOM();
    expect(w.nestedView?.state.doc.toString()).toBe('a\n\nb');
  });
});
