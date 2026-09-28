/** Tests for the shared in-app link contract (ref:/doc:/note: encode/decode/parse). */

import { describe, it, expect } from 'vitest';
import { parseInAppLink, encodeLinkId, decodeLinkId } from './in-app-link';

describe('parseInAppLink', () => {
  it('parses ref into type + id', () => {
    expect(parseInAppLink('ref:abc123')).toEqual({ type: 'ref', id: 'abc123' });
  });

  it('parses doc into type + id', () => {
    expect(parseInAppLink('doc:doc456')).toEqual({ type: 'doc', id: 'doc456' });
  });

  it('parses note into type + id', () => {
    expect(parseInAppLink('note:thread1')).toEqual({ type: 'note', id: 'thread1' });
  });

  it('returns null for non-in-app URLs', () => {
    expect(parseInAppLink('https://example.com')).toBeNull();
    expect(parseInAppLink('bare-id')).toBeNull();
  });
});

describe('encodeLinkId', () => {
  it('encodes doc as bare id (no prefix)', () => {
    expect(encodeLinkId('doc', 'doc456')).toBe('doc456');
  });

  it('encodes ref with scheme prefix', () => {
    expect(encodeLinkId('ref', 'abc')).toBe('ref:abc');
  });

  it('encodes note with scheme prefix', () => {
    expect(encodeLinkId('note', 'thread1')).toBe('note:thread1');
  });
});

describe('decodeLinkId', () => {
  it('decodes prefixed doc id to bare', () => {
    expect(decodeLinkId('doc', 'doc:doc456')).toBe('doc456');
  });

  it('decodes bare doc id as-is (defensive — no prefix to strip)', () => {
    expect(decodeLinkId('doc', 'doc456')).toBe('doc456');
  });

  it('decodes prefixed ref id to bare', () => {
    expect(decodeLinkId('ref', 'ref:abc')).toBe('abc');
  });

  it('decodes bare ref id as-is (defensive)', () => {
    expect(decodeLinkId('ref', 'abc')).toBe('abc');
  });

  it('decodes prefixed note id to bare', () => {
    expect(decodeLinkId('note', 'note:thread1')).toBe('thread1');
  });
});

describe('round-trip encode → decode', () => {
  it('round-trips all types', () => {
    const cases: Array<{ type: 'ref' | 'doc' | 'note'; id: string }> = [
      { type: 'doc', id: 'doc456' },
      { type: 'ref', id: 'abc' },
      { type: 'note', id: 'thread1' },
    ];
    for (const { type, id } of cases) {
      const encoded = encodeLinkId(type, id);
      const decoded = decodeLinkId(type, encoded);
      expect(decoded).toBe(id);
    }
  });
});
