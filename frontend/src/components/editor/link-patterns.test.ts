/** Tests for link regex pattern factories. */

import { describe, it, expect } from 'vitest';
import { noteLink, refLink, docLink, extLink } from './link-patterns';

describe('noteLink', () => {
  it('matches [text](note:id)', () => {
    const m = noteLink().exec('see [my note](note:abc123) here');
    expect(m).not.toBeNull();
    expect(m![1]).toBe('my note');
    expect(m![2]).toBe('note:abc123');
  });

  it('does not match ref: links', () => {
    const m = noteLink().exec('[text](ref:abc)');
    expect(m).toBeNull();
  });

  it('matches [[text]](note:id) double brackets', () => {
    const m = noteLink().exec('see [[my note]](note:abc123) here');
    expect(m).not.toBeNull();
    expect(m![1]).toBe('my note');
    expect(m![2]).toBe('note:abc123');
  });
});

describe('refLink', () => {
  it('matches ![alt](ref:id) (embedded image)', () => {
    const m = refLink().exec('![photo](ref:img1)');
    expect(m).not.toBeNull();
    expect(m![0].startsWith('!')).toBe(true);
    expect(m![1]).toBe('photo');
    expect(m![2]).toBe('ref:img1');
  });

  it('matches [text](ref:id) (non-embedded)', () => {
    const m = refLink().exec('[source](ref:doc1)');
    expect(m).not.toBeNull();
    expect(m![1]).toBe('source');
  });

  it('does not match note: links', () => {
    const m = refLink().exec('[text](note:abc)');
    expect(m).toBeNull();
  });

  it('matches [[text]](ref:id) double brackets', () => {
    const m = refLink().exec('[[photo]](ref:img1)');
    expect(m).not.toBeNull();
    expect(m![1]).toBe('photo');
    expect(m![2]).toBe('ref:img1');
  });
});

describe('docLink', () => {
  it('matches [text](doc-id) internal link', () => {
    const m = docLink().exec('[page](abc-123)');
    expect(m).not.toBeNull();
    expect(m![1]).toBe('page');
    expect(m![2]).toBe('abc-123');
  });

  it('does not match http links', () => {
    expect(docLink().exec('[x](http://a.com)')).toBeNull();
  });

  it('does not match note: links', () => {
    expect(docLink().exec('[x](note:abc)')).toBeNull();
  });

  it('does not match ref: links', () => {
    expect(docLink().exec('[x](ref:abc)')).toBeNull();
  });

  it('does not match mailto: links', () => {
    expect(docLink().exec('[x](mailto:a@b.c)')).toBeNull();
  });

  it('does not match anchor links', () => {
    expect(docLink().exec('[x](#section)')).toBeNull();
  });

  it('matches [text](doc:id) and strips doc: prefix', () => {
    const m = docLink().exec('[page](doc:abc-123)');
    expect(m).not.toBeNull();
    expect(m![1]).toBe('page');
    expect(m![2]).toBe('abc-123');
  });

  it('matches [text](id) without prefix unchanged', () => {
    const m = docLink().exec('[page](abc-123)');
    expect(m).not.toBeNull();
    expect(m![2]).toBe('abc-123');
  });

  it('matches [[text]](uuid) double brackets', () => {
    const m = docLink().exec('[[page]](abc-123)');
    expect(m).not.toBeNull();
    expect(m![1]).toBe('page');
    expect(m![2]).toBe('abc-123');
  });

  it('does not match [[text]](http://...) for docLink', () => {
    expect(docLink().exec('[[x]](http://a.com)')).toBeNull();
  });

  it('matches [[text]](doc:id) and strips doc: prefix', () => {
    const m = docLink().exec('[[page]](doc:abc-123)');
    expect(m).not.toBeNull();
    expect(m![1]).toBe('page');
    expect(m![2]).toBe('abc-123');
  });
});

describe('extLink', () => {
  it('matches [text](http://...)', () => {
    const m = extLink().exec('[site](http://example.com)');
    expect(m).not.toBeNull();
    expect(m![1]).toBe('site');
    expect(m![2]).toBe('http://example.com');
  });

  it('matches https links', () => {
    const m = extLink().exec('[s](https://a.com/path)');
    expect(m).not.toBeNull();
    expect(m![2]).toBe('https://a.com/path');
  });

  it('does not match internal links', () => {
    expect(extLink().exec('[x](abc-123)')).toBeNull();
  });

  it('matches [[text]](https://...) double brackets', () => {
    const m = extLink().exec('[[site]](https://example.com)');
    expect(m).not.toBeNull();
    expect(m![1]).toBe('site');
    expect(m![2]).toBe('https://example.com');
  });

  it('matches [[text]](http://...) double brackets', () => {
    const m = extLink().exec('[[site]](http://example.com)');
    expect(m).not.toBeNull();
    expect(m![1]).toBe('site');
    expect(m![2]).toBe('http://example.com');
  });
});

describe('each factory returns a fresh regex', () => {
  it('noteLink resets lastIndex', () => {
    const r1 = noteLink();
    const r2 = noteLink();
    r1.exec('[a](note:1) [b](note:2)');
    expect(r2.lastIndex).toBe(0);
  });
});
