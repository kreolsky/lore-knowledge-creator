/** Tests for useReferenceDelete batching behaviour.
 *
 * Covers: size-threshold flush, idle-debounce flush, single-in-flight chaining,
 * sendBeacon on unmount.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

import type { Reference } from '../types';

let container: HTMLDivElement;
let root: Root;
let postMock: ReturnType<typeof vi.fn>;
let sendBeaconMock: ReturnType<typeof vi.fn>;
let useReferenceDelete: typeof import('./useReferenceDelete').useReferenceDelete;

const makeRef = (id: string): Reference => ({
  reference_id: id,
  project_id: 'p',
  document_id: 'd',
  title: `ref-${id}`,
  media_type: 'markdown',
  source_url: '',
  content: '',
  processing_status: null,
  file_path: null,
  file_meta: null,
  headings: [],
  created_at: '',
  updated_at: '',
});

let scheduleDeleteRef: { current: ((ref: Reference) => void) | null };
let flushRef: { current: (() => Promise<void>) | null };

function TestHarness() {
  const hook = useReferenceDelete();
  scheduleDeleteRef.current = hook.scheduleDelete;
  flushRef.current = hook.flush;
  return null;
}

beforeEach(async () => {
  vi.resetModules();
  vi.useFakeTimers();

  postMock = vi.fn().mockResolvedValue({ deleted: 0, skipped: 0 });
  vi.doMock('../api/client', () => ({
    apiClient: { post: postMock },
  }));

  // Reset store so deletedRefIds doesn't leak between tests.
  const { useAppStore } = await import('../store/app-store');
  useAppStore.setState({ deletedRefIds: new Set(), references: [] });

  const mod = await import('./useReferenceDelete');
  useReferenceDelete = mod.useReferenceDelete;

  sendBeaconMock = vi.fn().mockReturnValue(true);
  Object.defineProperty(navigator, 'sendBeacon', {
    configurable: true,
    value: sendBeaconMock,
  });

  scheduleDeleteRef = { current: null };
  flushRef = { current: null };

  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root.render(createElement(TestHarness)));
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.useRealTimers();
  vi.doUnmock('../api/client');
});

describe('useReferenceDelete', () => {
  it('flushes immediately when threshold (5 IDs) is reached', async () => {
    for (let i = 0; i < 5; i++) {
      act(() => scheduleDeleteRef.current!(makeRef(`r${i}`)));
    }
    // Allow microtasks to settle (flush is async).
    await act(async () => { await Promise.resolve(); });

    expect(postMock).toHaveBeenCalledTimes(1);
    expect(postMock.mock.calls[0][1]).toEqual({
      reference_ids: ['r0', 'r1', 'r2', 'r3', 'r4'],
    });
  });

  it('flushes on idle (after 800ms) when below threshold', async () => {
    act(() => scheduleDeleteRef.current!(makeRef('a')));
    act(() => scheduleDeleteRef.current!(makeRef('b')));
    act(() => scheduleDeleteRef.current!(makeRef('c')));

    expect(postMock).not.toHaveBeenCalled();

    await act(async () => {
      vi.advanceTimersByTime(800);
      await Promise.resolve();
    });

    expect(postMock).toHaveBeenCalledTimes(1);
    expect(postMock.mock.calls[0][1]).toEqual({ reference_ids: ['a', 'b', 'c'] });
  });

  it('chains a second batch with IDs accumulated while POST was in flight', async () => {
    let resolveFirst!: (v: unknown) => void;
    postMock.mockImplementationOnce(
      () => new Promise(resolve => { resolveFirst = resolve; }),
    );
    postMock.mockResolvedValueOnce({ deleted: 0, skipped: 0 });

    // First batch via threshold trigger.
    for (let i = 0; i < 5; i++) {
      act(() => scheduleDeleteRef.current!(makeRef(`r${i}`)));
    }
    await act(async () => { await Promise.resolve(); });

    expect(postMock).toHaveBeenCalledTimes(1);

    // While first POST is unresolved, queue more.
    act(() => scheduleDeleteRef.current!(makeRef('x')));
    act(() => scheduleDeleteRef.current!(makeRef('y')));

    // Second flush should NOT fire yet — first still in flight.
    expect(postMock).toHaveBeenCalledTimes(1);

    // Resolve first POST.
    await act(async () => {
      resolveFirst({ deleted: 5, skipped: 0 });
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(postMock).toHaveBeenCalledTimes(2);
    expect(postMock.mock.calls[1][1]).toEqual({ reference_ids: ['x', 'y'] });
  });

  it('sendBeacon on page unload (pagehide) delivers pending IDs', async () => {
    act(() => scheduleDeleteRef.current!(makeRef('a')));
    act(() => scheduleDeleteRef.current!(makeRef('b')));

    act(() => { window.dispatchEvent(new Event('pagehide')); });

    expect(sendBeaconMock).toHaveBeenCalledTimes(1);
    const [url, body] = sendBeaconMock.mock.calls[0];
    expect(url).toBe('/api/references/batch-delete');
    expect(body).toBeInstanceOf(Blob);
    const text = await (body as Blob).text();
    expect(JSON.parse(text)).toEqual({ reference_ids: ['a', 'b'] });
  });

  it('unmount on live page uses normal POST (not sendBeacon)', async () => {
    act(() => scheduleDeleteRef.current!(makeRef('a')));
    act(() => scheduleDeleteRef.current!(makeRef('b')));

    act(() => root.unmount());
    await act(async () => { await Promise.resolve(); });

    expect(sendBeaconMock).not.toHaveBeenCalled();
    expect(postMock).toHaveBeenCalledTimes(1);
    expect(postMock.mock.calls[0][1]).toEqual({ reference_ids: ['a', 'b'] });
  });
});
