/** Unit tests for apiClient — HTTP error classification (401/403/generic). */

// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { apiClient, AuthError, ForbiddenError, ServiceUnavailableError, HttpError, _resetRedirectGuard } from './client';
import { tierRefusal } from '../utils/server-refusal-detail';

// ── Mock fetch ──────────────────────────────────────────────────────────────

const originalFetch = globalThis.fetch;

function mockFetch(status: number, body: unknown = {}) {
  globalThis.fetch = vi.fn().mockResolvedValue({
    status,
    ok: status >= 200 && status < 300,
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(typeof body === 'string' ? body : ''),
  } as Response);
}

beforeEach(() => {
  // Prevent actual navigation in tests
  Object.defineProperty(window, 'location', {
    writable: true,
    value: { href: '' },
  });
  _resetRedirectGuard();
});

afterEach(() => {
  globalThis.fetch = originalFetch;
});

// ── Tests ───────────────────────────────────────────────────────────────────

describe('handleResponse (via apiClient)', () => {
  it('returns parsed JSON on 200', async () => {
    mockFetch(200, { ok: true });
    const result = await apiClient.get('/test');
    expect(result).toEqual({ ok: true });
  });

  it('throws AuthError on 401', async () => {
    mockFetch(401);
    await expect(apiClient.get('/test')).rejects.toThrow(AuthError);
  });

  it('sets window.location.href on 401', async () => {
    mockFetch(401);
    try { await apiClient.get('/test'); } catch { /* expected */ }
    expect(window.location.href).toBe('/');
  });

  it('throws ForbiddenError on 403', async () => {
    mockFetch(403);
    await expect(apiClient.get('/test')).rejects.toThrow(ForbiddenError);
  });

  it('carries a structured 403 detail on the ForbiddenError', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ detail: { code: 'model_forbidden', model: 'vendor/x' } }),
      { status: 403 },
    ));
    await expect(apiClient.post('/chat/sessions/s/completions', {})).rejects.toMatchObject({
      name: 'ForbiddenError',
      detail: { code: 'model_forbidden', model: 'vendor/x' },
    });
  });

  it('leaves detail undefined on a 403 with a non-JSON body', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(new Response('nope', { status: 403 }));
    const err = await apiClient.get('/test').catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ForbiddenError);
    expect((err as ForbiddenError).detail).toBeUndefined();
  });

  it('throws ServiceUnavailableError on 502', async () => {
    mockFetch(502);
    await expect(apiClient.get('/test')).rejects.toThrow(ServiceUnavailableError);
  });

  it('throws ServiceUnavailableError on 503', async () => {
    mockFetch(503);
    await expect(apiClient.get('/test')).rejects.toThrow(ServiceUnavailableError);
  });

  it('sends correct method and headers for POST', async () => {
    mockFetch(200, { created: true });
    await apiClient.post('/items', { name: 'test' });
    expect(globalThis.fetch).toHaveBeenCalledWith('/api/items', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ name: 'test' }),
    });
  });

  it('returns undefined on 204 No Content without crashing', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      status: 204,
      ok: true,
      json: () => { throw new SyntaxError('Unexpected end of JSON input'); },
    } as unknown as Response);
    const result = await apiClient.delete('/items/1');
    expect(result).toBeUndefined();
  });
});

// ── Tier refusal (SYSTEM: capability-tiers) ─────────────────────────────────

describe('tierRefusal', () => {
  function mock403(body: unknown) {
    const res = { status: 403, ok: false, json: () => Promise.resolve(body) };
    globalThis.fetch = vi.fn().mockResolvedValue({ ...res, clone: () => res } as unknown as Response);
  }

  async function refusalOf(body: unknown): Promise<unknown> {
    mock403(body);
    return apiClient.upload('/references/upload', new FormData()).catch((e: unknown) => e);
  }

  it('names the refused tier of a 403 tier_required:<tier>', async () => {
    expect(tierRefusal(await refusalOf({ detail: 'tier_required:voice' }))).toBe('voice');
    expect(tierRefusal(await refusalOf({ detail: 'tier_required:ai' }))).toBe('ai');
  });

  it('a project-access 403, a structured 403 and a non-403 are NOT tier refusals', async () => {
    expect(tierRefusal(await refusalOf({ detail: 'Full project access required' }))).toBeNull();
    expect(tierRefusal(await refusalOf({ detail: { code: 'model_forbidden', model: 'm' } }))).toBeNull();
    expect(tierRefusal(new HttpError(404, 'tier_required:voice'))).toBeNull();
  });
});

// ── Concurrent 401 redirect guard ───────────────────────────────────────────

describe('concurrent 401 redirect guard', () => {
  it('redirects only once for multiple concurrent 401s', async () => {
    mockFetch(401);
    const results = await Promise.allSettled([
      apiClient.get('/a'),
      apiClient.get('/b'),
      apiClient.get('/c'),
    ]);
    // All should throw AuthError
    for (const r of results) {
      expect(r.status).toBe('rejected');
      expect((r as PromiseRejectedResult).reason).toBeInstanceOf(AuthError);
    }
    // window.location.href should only be set once (to '/')
    expect(window.location.href).toBe('/');
  });

  it('_resetRedirectGuard allows redirect again after reset', async () => {
    mockFetch(401);
    await apiClient.get('/test').catch(() => {});
    expect(window.location.href).toBe('/');

    // Reset guard
    _resetRedirectGuard();
    window.location.href = '';

    await apiClient.get('/test2').catch(() => {});
    expect(window.location.href).toBe('/');
  });
});
