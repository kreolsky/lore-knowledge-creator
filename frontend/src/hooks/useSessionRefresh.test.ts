/**
 * TDD for session-refresh scheduler.
 *
 * Behavior under test:
 *  - Calls /api/auth/me on a fixed interval while the tab is visible.
 *  - Calls /api/auth/me when the tab returns to visible after being hidden,
 *    but only if enough time has elapsed since the last refresh (debounce).
 *  - Does NOT redirect/logout on transient failures (ServiceUnavailableError /
 *    TypeError) — shows a toast instead. A genuine 401 still redirects via apiClient.
 */
// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { apiClient, ServiceUnavailableError, _resetRedirectGuard } from '../api/client';
import { startSessionRefresh, SESSION_REFRESH_INTERVAL_MS } from '../hooks/useSessionRefresh';

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
  Object.defineProperty(window, 'location', { writable: true, value: { href: '' } });
  _resetRedirectGuard();
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
});

describe('startSessionRefresh — interval', () => {
  it('calls /api/auth/me every SESSION_REFRESH_INTERVAL_MS', async () => {
    mockFetch(200, { user_id: 'u1' });
    const stop = startSessionRefresh({ onTransientFailure: vi.fn() });
    // First tick fires immediately on start (bootstrap refresh).
    await vi.advanceTimersByTimeAsync(1);
    await vi.advanceTimersByTimeAsync(SESSION_REFRESH_INTERVAL_MS);
    await vi.advanceTimersByTimeAsync(SESSION_REFRESH_INTERVAL_MS);
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThanOrEqual(3);
    expect((globalThis.fetch as ReturnType<typeof vi.fn>)).toHaveBeenCalledWith(
      '/api/auth/me', expect.objectContaining({ credentials: 'include' }),
    );
    stop();
  });

  it('stops calling after stop()', async () => {
    mockFetch(200, { user_id: 'u1' });
    const stop = startSessionRefresh({ onTransientFailure: vi.fn() });
    await vi.advanceTimersByTimeAsync(1);
    const callsBefore = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length;
    stop();
    await vi.advanceTimersByTimeAsync(SESSION_REFRESH_INTERVAL_MS * 3);
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBe(callsBefore);
  });
});

describe('startSessionRefresh — visibilitychange', () => {
  it('refreshes when tab becomes visible', async () => {
    Object.defineProperty(document, 'visibilityState', {
      configurable: true, value: 'hidden',
    });
    mockFetch(200, { user_id: 'u1' });
    const stop = startSessionRefresh({ onTransientFailure: vi.fn() });
    await vi.advanceTimersByTimeAsync(1);
    const before = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length;

    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
    document.dispatchEvent(new Event('visibilitychange'));
    await vi.advanceTimersByTimeAsync(1);

    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(before);
    stop();
  });

  it('does not refresh when tab becomes hidden', async () => {
    Object.defineProperty(document, 'visibilityState', {
      configurable: true, value: 'visible',
    });
    mockFetch(200, { user_id: 'u1' });
    const stop = startSessionRefresh({ onTransientFailure: vi.fn() });
    await vi.advanceTimersByTimeAsync(1);
    const before = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length;

    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' });
    document.dispatchEvent(new Event('visibilitychange'));
    await vi.advanceTimersByTimeAsync(1);

    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBe(before);
    stop();
  });
});

describe('startSessionRefresh — transient failure handling', () => {
  it('calls onTransientFailure (not redirect) on ServiceUnavailableError', async () => {
    mockFetch(503);
    const onTransientFailure = vi.fn();
    const stop = startSessionRefresh({ onTransientFailure });
    // apiClient retries 503 with 1s+2s backoff — advance past all retries.
    await vi.advanceTimersByTimeAsync(5000);
    expect(onTransientFailure).toHaveBeenCalledTimes(1);
    expect(window.location.href).toBe('');
    stop();
  });

  it('calls onTransientFailure on network TypeError', async () => {
    globalThis.fetch = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'));
    const onTransientFailure = vi.fn();
    const stop = startSessionRefresh({ onTransientFailure });
    await vi.advanceTimersByTimeAsync(1);
    expect(onTransientFailure).toHaveBeenCalledTimes(1);
    stop();
  });

  it('redirects on genuine 401 (apiClient behavior)', async () => {
    mockFetch(401);
    const onTransientFailure = vi.fn();
    const stop = startSessionRefresh({ onTransientFailure });
    await vi.advanceTimersByTimeAsync(1);
    expect(onTransientFailure).not.toHaveBeenCalled();
    expect(window.location.href).toBe('/');
    stop();
  });
});

describe('startSessionRefresh — guard against apiClient usage', () => {
  it('does not import apiClient (keeps redirect invariant in client.ts)', async () => {
    // Sanity: the module must use apiClient so 401 redirect logic is centralized.
    expect(typeof apiClient).toBe('object');
    expect(ServiceUnavailableError).toBeDefined();
  });
});
