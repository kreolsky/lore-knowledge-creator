/** Tests for link regex pattern factories. */

import { describe, it, expect } from 'vitest';
import { refLink, docLink, extLink } from './link-patterns';

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
  it('refLink resets lastIndex', () => {
    const r1 = refLink();
    const r2 = refLink();
    r1.exec('[a](ref:1) [b](ref:2)');
    expect(r2.lastIndex).toBe(0);
  });
});
