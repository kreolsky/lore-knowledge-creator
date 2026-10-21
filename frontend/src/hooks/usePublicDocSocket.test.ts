/**
 * usePublicDocSocket — the public share's change-ping socket.
 *
 * Fake WebSocket (MockWebSocket) + fake timers over the mocked notes store:
 * the changed→refetch contract, client-side coalescing (one in flight + one
 * trailing), hidden-tab deferral, catch-up on (re)open and on drops, permanent
 * close vs reconnect vs give-up, and the StrictMode double-mount re-entry
 * guard.
 */
// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { MockWebSocket } from '../collab/__tests__/mock-ws';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const store = vi.hoisted(() => ({
  documentId: 'doc-1' as string | null,
  behind: false,
  load: vi.fn(async () => {}),
  refreshText: vi.fn(async () => true),
  setLiveLost: vi.fn(),
}));

vi.mock('../store/public-notes-store', () => ({
  // The hook never subscribes at render time — getState inside effects only.
  usePublicNotesStore: Object.assign(() => ({}), { getState: () => store }),
  publicDocIsBehind: () => store.behind,
}));

import { usePublicDocSocket } from './usePublicDocSocket';

function setVisibility(v: 'visible' | 'hidden') {
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: v });
}

/** Flush pending microtasks (the refetch chain) without touching fake time. */
async function flush() {
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
}

/** One opened socket: renders the hook, connects, opens. */
function openHook(docId = 'doc-1', enabled = true) {
  const rendered = renderHook(({ id, on }) => usePublicDocSocket(id, on), {
    initialProps: { id: docId, on: enabled },
  });
  const ws = MockWebSocket.latest();
  act(() => ws.simulateOpen());
  return { rendered, ws };
}

beforeEach(() => {
  MockWebSocket.reset();
  vi.stubGlobal('WebSocket', MockWebSocket);
  vi.useFakeTimers();
  // Reconnect jitter (up to 500ms) would break exact-timing assertions; pin it.
  vi.spyOn(Math, 'random').mockReturnValue(0);
  setVisibility('visible');
  store.documentId = 'doc-1';
  store.behind = false;
  store.load = vi.fn(async () => {});
  store.refreshText = vi.fn(async () => true);
  store.setLiveLost = vi.fn();
});

afterEach(() => {
  act(() => vi.clearAllMocks());
  vi.restoreAllMocks();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  setVisibility('visible');
});

describe('usePublicDocSocket', () => {
  it('creates no socket while disabled (comments off)', () => {
    renderHook(() => usePublicDocSocket('doc-1', false));
    expect(MockWebSocket.instances).toHaveLength(0);
  });

  it('connects to /ws/public/doc/<id> and refetches on changed', async () => {
    const { rendered, ws } = openHook();
    expect(ws.url).toContain('/ws/public/doc/doc-1');
    act(() => ws.simulateMessage({ type: 'changed' }));
    await flush();
    expect(store.load).toHaveBeenCalledTimes(1);
    expect(store.load).toHaveBeenCalledWith('doc-1');
    // The list matched the served text: no text re-read.
    expect(store.refreshText).not.toHaveBeenCalled();
    rendered.unmount();
  });

  it('re-reads the text when the list came back ahead of the editor', async () => {
    store.behind = true;
    const { rendered, ws } = openHook();
    act(() => ws.simulateMessage({ type: 'changed' }));
    await flush();
    expect(store.load).toHaveBeenCalledTimes(1);
    expect(store.refreshText).toHaveBeenCalledWith('doc-1');
    rendered.unmount();
  });

  it('coalesces: 3 pings during one in-flight run → exactly one trailing run', async () => {
    let release!: () => void;
    store.load.mockImplementation(() => new Promise<void>(r => { release = r; }));
    const { rendered, ws } = openHook();
    act(() => ws.simulateMessage({ type: 'changed' }));   // run 1 starts
    act(() => ws.simulateMessage({ type: 'changed' }));   // trailing queued
    act(() => ws.simulateMessage({ type: 'changed' }));   // still queued — one slot
    await flush();
    expect(store.load).toHaveBeenCalledTimes(1);
    await act(async () => { release(); });
    await flush();
    expect(store.load).toHaveBeenCalledTimes(2);
    await act(async () => { release(); }); // settle the trailing run before unmount
    rendered.unmount();
  });

  it('reconnects and catches up: transient close (1006) → one refetch + reconnect, reopen → one refetch', async () => {
    const { rendered, ws } = openHook();
    act(() => ws.simulateClose(1006));
    await flush();
    expect(store.load).toHaveBeenCalledTimes(1);      // catch-up on the drop
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(MockWebSocket.instances).toHaveLength(2);  // reconnect scheduled + fired
    const ws2 = MockWebSocket.latest();
    act(() => ws2.simulateOpen());
    await flush();
    expect(store.load).toHaveBeenCalledTimes(2);      // catch-up on reopen
    rendered.unmount();
  });

  it('permanent close (4004) → one refetch, no reconnect, no liveLost', async () => {
    const { rendered, ws } = openHook();
    act(() => ws.simulateClose(4004));
    await flush();
    expect(store.load).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(MockWebSocket.instances).toHaveLength(1);  // never reconnected
    // Only the mount-time reset (false) ran — a permanent close never reports
    // the give-up loss.
    expect(store.setLiveLost).not.toHaveBeenCalledWith(true);
    rendered.unmount();
  });

  it('reconnect give-up sets liveLost', async () => {
    const rendered = renderHook(() => usePublicDocSocket('doc-1', true));
    // MAX_RECONNECT_ATTEMPTS (50) scheduled closes, each reconnecting; the
    // next close past the cap gives up and reports the loss.
    for (let i = 0; i <= 50; i++) {
      const ws = MockWebSocket.latest();
      act(() => ws.simulateClose(1006));
      await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    }
    expect(store.setLiveLost).toHaveBeenCalledWith(true);
    rendered.unmount();
  });

  it('60 s of heartbeats with no message never calls load', async () => {
    const { rendered, ws } = openHook();
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(store.load).not.toHaveBeenCalled();
    // The idle tab's cost is the 30 s heartbeat, nothing else.
    expect(ws.sent.filter(s => s.includes('heartbeat')).length).toBeGreaterThanOrEqual(2);
    rendered.unmount();
  });

  it('a ping while hidden defers; one visibilitychange → visible runs exactly one load', async () => {
    const { rendered, ws } = openHook();
    setVisibility('hidden');
    act(() => ws.simulateMessage({ type: 'changed' }));
    act(() => ws.simulateMessage({ type: 'changed' }));
    await flush();
    expect(store.load).not.toHaveBeenCalled();
    setVisibility('visible');
    await act(async () => { document.dispatchEvent(new Event('visibilitychange')); });
    await flush();
    expect(store.load).toHaveBeenCalledTimes(1);
    rendered.unmount();
  });

  it('visibilitychange → visible with no deferred ping loads nothing', async () => {
    const { rendered } = openHook();
    setVisibility('hidden');
    await act(async () => { document.dispatchEvent(new Event('visibilitychange')); });
    setVisibility('visible');
    await act(async () => { document.dispatchEvent(new Event('visibilitychange')); });
    await flush();
    expect(store.load).not.toHaveBeenCalled();
    rendered.unmount();
  });

  it('a healthy open resets the backoff: repeated drop→reopen cycles keep reconnecting', async () => {
    const rendered = renderHook(() => usePublicDocSocket('doc-1', true));
    for (let cycle = 0; cycle < 3; cycle++) {
      const ws = MockWebSocket.latest();
      act(() => ws.simulateOpen());
      act(() => ws.simulateClose(1006));
      await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    }
    // Three healthy cycles later the controller still has all its attempts —
    // no premature give-up (setLiveLost never fires) and a socket exists.
    expect(store.setLiveLost).not.toHaveBeenCalledWith(true);
    expect(MockWebSocket.instances.length).toBe(4); // initial + 3 reconnects
    rendered.unmount();
  });

  it('double mount (StrictMode) leaves exactly one open socket', () => {
    const first = renderHook(() => usePublicDocSocket('doc-1', true));
    first.unmount(); // dev StrictMode: effect → cleanup → effect
    const second = renderHook(() => usePublicDocSocket('doc-1', true));
    act(() => MockWebSocket.latest().simulateOpen());
    expect(MockWebSocket.instances.filter(i => i.readyState === 1)).toHaveLength(1);
    second.unmount();
  });
});
