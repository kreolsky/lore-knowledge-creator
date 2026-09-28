/**
 * Tests for the link-types registry — single source of link-type knowledge.
 * Per entry: match/resolve/validate; class+tooltip; broken-toast path.
 */

// @vitest-environment jsdom
import { describe, it, expect, beforeEach, vi } from 'vitest';
import {
  LINK_TYPES,
  resolveLinkClass,
  linkBrokenTooltip,
  showBrokenToast,
  type LinkTypeEntry,
} from './link-types';
import {
  validDocIds,
  validNoteThreadIds,
  validRefIds,
  projectRefIds,
} from './link-validity';

const entry = (kind: LinkTypeEntry['kind']) =>
  LINK_TYPES.find((e) => e.kind === kind)!;

describe('link-types registry — match + resolve (normalized shape)', () => {
  it('note: matches and strips the note: prefix from id', () => {
    const ms = entry('note').match('see [hello](note:abc123)');
    expect(ms).toHaveLength(1);
    expect(ms[0].id).toBe('abc123');
    expect(ms[0].raw).toContain('note:abc123');
    expect(ms[0].start).toBeTypeOf('number');
    expect(ms[0].end).toBe(ms[0].start + ms[0].raw.length);
  });

  it('ref: matches and strips the ref: prefix from id', () => {
    const ms = entry('ref').match('see [r](ref:ref-9)');
    expect(ms).toHaveLength(1);
    expect(ms[0].id).toBe('ref-9');
  });

  it('doc: matches a bare-id link (no prefix to strip)', () => {
    const ms = entry('doc').match('[d](doc-42)');
    expect(ms).toHaveLength(1);
    expect(ms[0].id).toBe('doc-42');
  });

  it('ext: matches a [label](http...) link and id is the url', () => {
    const ms = entry('ext').match('[ex](https://example.com)');
    expect(ms).toHaveLength(1);
    expect(ms[0].id).toBe('https://example.com');
  });

  it('bare: matches a bare url and id is the url (group[0])', () => {
    const ms = entry('bare').match('visit https://example.com now');
    expect(ms).toHaveLength(1);
    expect(ms[0].id).toBe('https://example.com');
  });

  it('extractId strips the scheme prefix consistently with match id', () => {
    expect(entry('note').extractId('note:abc')).toBe('abc');
    expect(entry('ref').extractId('ref:xyz')).toBe('xyz');
    expect(entry('doc').extractId('doc-1')).toBe('doc-1');
    expect(entry('ext').extractId('https://x.io')).toBe('https://x.io');
  });

  it('each match returns the normalized { start, end, raw, id } shape', () => {
    for (const e of LINK_TYPES) {
      const ms = e.match('x');
      for (const m of ms) {
        expect(m).toHaveProperty('start');
        expect(m).toHaveProperty('end');
        expect(m).toHaveProperty('raw');
        expect(m).toHaveProperty('id');
      }
    }
  });
});

describe('link-types registry — validate', () => {
  beforeEach(() => {
    validDocIds.clear();
    validNoteThreadIds.clear();
    validRefIds.clear();
    projectRefIds.clear();
  });

  it('note validate reads validNoteThreadIds', () => {
    expect(entry('note').validate('n1')).toBe(false);
    validNoteThreadIds.add('n1');
    expect(entry('note').validate('n1')).toBe(true);
  });

  it('ref validate reads validRefIds OR projectRefIds', () => {
    expect(entry('ref').validate('r1')).toBe(false);
    validRefIds.add('r1');
    expect(entry('ref').validate('r1')).toBe(true);
    validRefIds.delete('r1');
    projectRefIds.add('r1');
    expect(entry('ref').validate('r1')).toBe(true);
  });

  it('doc validate reads validDocIds', () => {
    expect(entry('doc').validate('d1')).toBe(false);
    validDocIds.add('d1');
    expect(entry('doc').validate('d1')).toBe(true);
  });

  it('ext and bare are always valid (no validity set)', () => {
    expect(entry('ext').validate('https://x.io')).toBe(true);
    expect(entry('bare').validate('https://x.io')).toBe(true);
  });
});

describe('link-types registry — decorationClass + brokenTooltip', () => {
  it('each entry exposes a base decoration class', () => {
    expect(entry('note').decorationClass).toBe('cm-note-link');
    expect(entry('ref').decorationClass).toBe('cm-ref-link');
    expect(entry('doc').decorationClass).toBe('cm-doc-link');
    expect(entry('ext').decorationClass).toBe('cm-ext-link');
    expect(entry('bare').decorationClass).toBe('cm-ext-link');
  });

  it('brokenTooltip is non-empty for resolvable types, empty for ext/bare', () => {
    expect(entry('note').brokenTooltip()).toBeTruthy();
    expect(entry('ref').brokenTooltip()).toBeTruthy();
    expect(entry('doc').brokenTooltip()).toBeTruthy();
    expect(entry('ext').brokenTooltip()).toBe('');
    expect(entry('bare').brokenTooltip()).toBe('');
  });
});

describe('resolveLinkClass — delegated validity for build-structural', () => {
  beforeEach(() => {
    validDocIds.clear();
    validNoteThreadIds.clear();
    validRefIds.clear();
    projectRefIds.clear();
  });

  it('appends -broken when invalid, returns base class when valid', () => {
    expect(resolveLinkClass('cm-doc-link', 'doc-1')).toBe('cm-doc-link-broken');
    validDocIds.add('doc-1');
    expect(resolveLinkClass('cm-doc-link', 'doc-1')).toBe('cm-doc-link');
  });

  it('note/ref strip their prefix before validating', () => {
    expect(resolveLinkClass('cm-note-link', 'note:n1')).toBe('cm-note-link-broken');
    validNoteThreadIds.add('n1');
    expect(resolveLinkClass('cm-note-link', 'note:n1')).toBe('cm-note-link');

    expect(resolveLinkClass('cm-ref-link', 'ref:r1')).toBe('cm-ref-link-broken');
    validRefIds.add('r1');
    expect(resolveLinkClass('cm-ref-link', 'ref:r1')).toBe('cm-ref-link');
  });

  it('returns the class unchanged for unknown classes (plain cm-link)', () => {
    expect(resolveLinkClass('cm-link', 'whatever')).toBe('cm-link');
  });

  it('ext/bare classes never go broken', () => {
    expect(resolveLinkClass('cm-ext-link', 'https://x.io')).toBe('cm-ext-link');
  });
});

describe('linkBrokenTooltip — delegated tooltip for build-structural', () => {
  it('maps a -broken class back to its tooltip', () => {
    expect(linkBrokenTooltip('cm-doc-link-broken')).toBe(entry('doc').brokenTooltip());
    expect(linkBrokenTooltip('cm-note-link-broken')).toBe(entry('note').brokenTooltip());
    expect(linkBrokenTooltip('cm-ref-link-broken')).toBe(entry('ref').brokenTooltip());
  });

  it('returns "" for non-broken / unknown classes', () => {
    expect(linkBrokenTooltip('cm-doc-link')).toBe('');
    expect(linkBrokenTooltip('cm-ext-link')).toBe('');
    expect(linkBrokenTooltip('cm-link')).toBe('');
  });
});

describe('showBrokenToast', () => {
  beforeEach(() => {
    validDocIds.clear();
    validNoteThreadIds.clear();
    validRefIds.clear();
    projectRefIds.clear();
    vi.clearAllMocks();
  });

  it('shows a toast for a broken resolvable type', async () => {
    const { useAppStore } = await import('../../../store/app-store');
    const spy = vi.spyOn(useAppStore.getState(), 'showToast');
    showBrokenToast('cm-note-link');
    expect(spy).toHaveBeenCalledWith(entry('note').brokenTooltip(), 'warning');
    spy.mockRestore();
  });

  it('no-ops for ext/bare (empty tooltip)', async () => {
    const { useAppStore } = await import('../../../store/app-store');
    const spy = vi.spyOn(useAppStore.getState(), 'showToast');
    showBrokenToast('cm-ext-link');
    expect(spy).not.toHaveBeenCalled();
    spy.mockRestore();
  });
});
