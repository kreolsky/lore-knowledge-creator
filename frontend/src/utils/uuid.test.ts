/** uuid() — RFC4122 v4 with a fallback for insecure origins. */
import { describe, it, expect, vi, afterEach } from 'vitest';

import { uuid } from './uuid';

const V4_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

afterEach(() => {
  vi.restoreAllMocks();
});

describe('uuid', () => {
  it('returns a v4 UUID string', () => {
    expect(uuid()).toMatch(V4_RE);
  });

  it('never repeats (distinct ids for distinct calls)', () => {
    const seen = new Set(Array.from({ length: 200 }, () => uuid()));
    expect(seen.size).toBe(200);
  });

  it('falls back to Web Crypto getRandomValues when randomUUID is missing (insecure origin)', () => {
    const getRandomValues = vi.fn(() => new Uint8Array(16).fill(7));
    // @ts-expect-error deleting the member to simulate an insecure origin
    delete globalThis.crypto.randomUUID;
    vi.stubGlobal('crypto', { getRandomValues });

    expect(uuid()).toMatch(V4_RE);
    expect(getRandomValues).toHaveBeenCalledTimes(1);
  });

  it('falls back to Math.random when no Web Crypto at all', () => {
    vi.stubGlobal('crypto', undefined);
    expect(uuid()).toMatch(V4_RE);
  });
});
