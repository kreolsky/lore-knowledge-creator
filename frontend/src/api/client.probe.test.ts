/** apiClient.probe — non-redirecting status probe (plan "public-document-ids").
 *
 * The /docs/:id decider must distinguish "no session" (401) from "session but no
 * access / not found" WITHOUT apiClient's redirect-on-401 guard firing (which
 * would kick an anonymous visitor off /docs/:id to '/'). probe() resolves 401 to
 * { ok:false } instead. */

// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { apiClient, _resetRedirectGuard, redirecting } from './client';

const originalFetch = globalThis.fetch;

function mockFetch(status: number, body?: unknown) {
  const res = {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => '',
  };
  globalThis.fetch = vi.fn(async () => res as unknown as Response) as unknown as typeof fetch;
}

beforeEach(() => {
  _resetRedirectGuard();
});

afterEach(() => {
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
});

describe('apiClient.probe', () => {
  it('returns {ok:true, data} on 200', async () => {
    mockFetch(200, { user_id: 'u1' });
    const r = await apiClient.probe('/auth/me');
    expect(r).toEqual({ ok: true, status: 200, data: { user_id: 'u1' } });
  });

  it('returns {ok:false, status:401} on 401 WITHOUT redirecting to "/"', async () => {
    // The whole point: no window.location redirect on the anonymous branch. A
    // real get() would set `redirecting=true` and throw AuthError; probe resolves
    // instead. `redirecting` staying false is the redirect-didn't-fire proof.
    mockFetch(401);
    const r = await apiClient.probe('/auth/me');
    expect(r).toEqual({ ok: false, status: 401 });
    expect(redirecting).toBe(false);
  });

  it('returns {ok:false} on 404 (uniform anonymous 404)', async () => {
    mockFetch(404);
    const r = await apiClient.probe('/documents/open/missing');
    expect(r).toEqual({ ok: false, status: 404 });
  });
});
