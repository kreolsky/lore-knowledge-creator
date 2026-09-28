/**
 * TDD for referenceThumbUrl — the public-aware thumb-URL builder.
 *
 * Mirrors the existing `referenceFileUrl` public/authed switch: when the public
 * file context is set (PublicSharePage mount), thumbs route through the
 * anonymous surface; otherwise the authed one. The asymmetry vs
 * `referenceFileUrl(id, filePath)` is intentional: the thumb URL is a literal
 * `/thumb` segment (no basename), so `referenceThumbUrl` takes no path arg.
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import {
  referenceThumbUrl,
  referenceFileUrl,
  setPublicFileContext,
  getPublicFileContext,
} from './reference-url';

describe('referenceThumbUrl', () => {
  beforeEach(() => setPublicFileContext(null));
  afterEach(() => setPublicFileContext(null));

  it('returns the authed thumb path when no public context is set', () => {
    expect(referenceThumbUrl('ref-123')).toBe('/api/files/ref-123/thumb');
  });

  it('returns the public thumb path when a public context is set', () => {
    setPublicFileContext('root-doc-abc');
    expect(referenceThumbUrl('ref-123')).toBe('/api/public/documents/root-doc-abc/files/ref-123/thumb');
  });

  it('clears back to the authed path after the public context is unset', () => {
    setPublicFileContext('root-doc-abc');
    expect(referenceThumbUrl('ref-123')).toBe('/api/public/documents/root-doc-abc/files/ref-123/thumb');
    setPublicFileContext(null);
    expect(referenceThumbUrl('ref-123')).toBe('/api/files/ref-123/thumb');
  });

  it('is independent of referenceFileUrl (no path arg required)', () => {
    setPublicFileContext('root-doc-abc');
    // referenceFileUrl needs a filePath (basename extracted); referenceThumbUrl
    // takes only an id. The two must NOT silently cross-wire.
    expect(referenceFileUrl('ref-123', 'proj/ref-123/img.png')).toBe(
      '/api/public/documents/root-doc-abc/files/ref-123/img.png',
    );
    expect(referenceThumbUrl('ref-123')).toBe('/api/public/documents/root-doc-abc/files/ref-123/thumb');
  });
});

describe('referenceThumbUrl / setPublicFileContext interop', () => {
  beforeEach(() => setPublicFileContext(null));
  afterEach(() => setPublicFileContext(null));

  it('getPublicFileContext reflects the active public root document id', () => {
    expect(getPublicFileContext()).toBeNull();
    setPublicFileContext('root-doc-xyz');
    expect(getPublicFileContext()).toBe('root-doc-xyz');
  });
});
