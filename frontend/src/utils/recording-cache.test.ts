/**
 * Tests for the recording cache — round trip, liveness filtering (lock + updatedAt
 * paths), deletion surfaces, the boot sweep, the synchronous unsent counter, the
 * v2 per-chunk store's durability, and the no-IndexedDB memory fallback.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import 'fake-indexeddb/auto';

type Cache = typeof import('./recording-cache');

const DB_NAME = 'lore-recording-cache';

let cache: Cache;
/** Toast spy bound to the SAME module graph as `cache` (freshModule re-imports both). */
type ShowToast = (message: string, type?: 'info' | 'error' | 'warning') => void;
let toastSpy: ReturnType<typeof vi.fn<ShowToast>>;

/** Fresh module + empty database per test. */
async function freshModule(): Promise<void> {
  vi.resetModules();
  localStorage.clear();
  await new Promise<void>((resolve) => {
    const req = indexedDB.deleteDatabase(DB_NAME);
    req.onsuccess = req.onerror = req.onblocked = () => resolve();
  });
  cache = await import('./recording-cache');
  // Same fresh registry as the cache module → the store instance the cache
  // toasts through; swap its showToast for a spy.
  const { useAppStore } = await import('../store/app-store');
  toastSpy = vi.fn<ShowToast>(() => undefined);
  useAppStore.setState({ showToast: toastSpy });
}

beforeEach(async () => { await freshModule(); });
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe('recording-cache — no IndexedDB', () => {
  it('every call resolves, never throws', async () => {
    const real = globalThis.indexedDB;
    // @ts-expect-error — simulating a browser/private mode without IndexedDB
    delete globalThis.indexedDB;
    try {
      vi.resetModules();
      localStorage.clear();
      cache = await import('./recording-cache');

      await expect(cache.createSession('s1', 'p1', 'd1')).resolves.toBeUndefined();
      await expect(cache.appendChunk('s1', new Blob(['x']))).resolves.toBeUndefined();
      await expect(cache.finalizeSession('s1', 't.webm')).resolves.toBeUndefined();
      await expect(cache.deleteSession('s1')).resolves.toBeUndefined();
      await expect(cache.listRestorable()).resolves.toEqual([]);
      await expect(cache.clearAll()).resolves.toBeUndefined();
      expect(() => cache.hasUnsent()).not.toThrow();
    } finally {
      globalThis.indexedDB = real;
    }
  });
});
