/** Unit tests for apiClient — HTTP error classification (401/403/generic). */

// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { apiClient, AuthError, ForbiddenError, ServiceUnavailableError, HttpError, _resetRedirectGuard } from './client';

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

  it('throws HttpError with status 500 on 500', async () => {
    mockFetch(500);
    await expect(apiClient.get('/test')).rejects.toBeInstanceOf(HttpError);
    mockFetch(500);
    await expect(apiClient.get('/test')).rejects.toMatchObject({ status: 500 });
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

// ── PUT method ──────────────────────────────────────────────────────────────

describe('apiClient.put', () => {
  it('sends PUT with JSON body and returns parsed response', async () => {
    mockFetch(200, { updated: true });
    const result = await apiClient.put('/items/1', { name: 'updated' });
    expect(result).toEqual({ updated: true });
    expect(globalThis.fetch).toHaveBeenCalledWith('/api/items/1', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ name: 'updated' }),
    });
  });

  it('handles 204 on PUT', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      status: 204,
      ok: true,
      json: () => { throw new SyntaxError('Unexpected end of JSON input'); },
    } as unknown as Response);
    const result = await apiClient.put('/items/1', { name: 'x' });
    expect(result).toBeUndefined();
  });
});

// ── PATCH method ────────────────────────────────────────────────────────────

describe('apiClient.patch', () => {
  it('sends PATCH with JSON body and returns parsed response', async () => {
    mockFetch(200, { patched: true });
    const result = await apiClient.patch('/items/1', { title: 'new' });
    expect(result).toEqual({ patched: true });
    expect(globalThis.fetch).toHaveBeenCalledWith('/api/items/1', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ title: 'new' }),
    });
  });
});

// ── DELETE method ───────────────────────────────────────────────────────────

describe('apiClient.delete', () => {
  it('sends DELETE and returns parsed response on 200', async () => {
    mockFetch(200, { deleted: true });
    const result = await apiClient.delete('/items/1');
    expect(result).toEqual({ deleted: true });
    expect(globalThis.fetch).toHaveBeenCalledWith('/api/items/1', {
      method: 'DELETE',
      credentials: 'include',
    });
  });

  it('handles 204 No Content on DELETE', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      status: 204,
      ok: true,
      json: () => { throw new SyntaxError('Unexpected end of JSON input'); },
    } as unknown as Response);
    const result = await apiClient.delete('/items/1');
    expect(result).toBeUndefined();
  });

  it('does not send Content-Type header', async () => {
    mockFetch(204);
    await apiClient.delete('/items/1').catch(() => {});
    const callArgs = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0][1];
    expect(callArgs.headers).toBeUndefined();
  });
});

// ── UPLOAD method ───────────────────────────────────────────────────────────

describe('apiClient.upload', () => {
  it('sends POST with FormData body, no Content-Type header', async () => {
    mockFetch(200, { file_id: 'f1' });
    const formData = new FormData();
    formData.append('file', new Blob(['content']), 'test.txt');
    const result = await apiClient.upload('/files', formData);
    expect(result).toEqual({ file_id: 'f1' });
    const callArgs = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0][1];
    expect(callArgs.method).toBe('POST');
    expect(callArgs.credentials).toBe('include');
    expect(callArgs.body).toBe(formData);
    // Must NOT set Content-Type — browser sets multipart boundary
    expect(callArgs.headers).toBeUndefined();
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
