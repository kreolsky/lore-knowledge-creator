/**
 * Tests for the recording cache — round trip, liveness filtering (lock + updatedAt
 * paths), deletion surfaces, the boot sweep, the synchronous unsent counter, the
 * v2 per-chunk store's durability, and the no-IndexedDB memory fallback.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import 'fake-indexeddb/auto';

type Cache = typeof import('./recording-cache');

const DB_NAME = 'lore-recording-cache';
const LOCK_KEY = 'lore-recording-live';

let cache: Cache;

interface CachedRecord {
  id: string;
  projectId: string;
  documentId: string;
  createdAt: number;
  updatedAt: number;
  status: 'recording' | 'pending-upload';
  title?: string;
  chunkCount: number;
}

/** A raw chunk-store row, as the module persists it (compound key sessionId+seq). */
interface ChunkRow {
  sessionId: string;
  seq: number;
  data: Uint8Array;
}

/** Fresh module + empty database per test. */
async function freshModule(): Promise<void> {
  vi.resetModules();
  localStorage.clear();
  await new Promise<void>((resolve) => {
    const req = indexedDB.deleteDatabase(DB_NAME);
    req.onsuccess = req.onerror = req.onblocked = () => resolve();
  });
  cache = await import('./recording-cache');
}

/** Read/write the module's stores directly — the tests must not widen the module's API.
 * No version pin: the module owns the DB version; a pinned one would VersionError. */
async function withStore<T>(store: string, mode: IDBTransactionMode, fn: (s: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  const db = await new Promise<IDBDatabase>((resolve, reject) => {
    const req = indexedDB.open(DB_NAME);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
  try {
    return await new Promise<T>((resolve, reject) => {
      const tx = db.transaction(store, mode);
      const req = fn(tx.objectStore(store));
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  } finally {
    db.close();
  }
}

async function readAllRecords(): Promise<CachedRecord[]> {
  return withStore('recordings', 'readonly', s => s.getAll() as IDBRequest<CachedRecord[]>);
}

async function readChunkRows(): Promise<ChunkRow[]> {
  return withStore('recording-chunks', 'readonly', s => s.getAll() as IDBRequest<ChunkRow[]>);
}

async function patchRecord(id: string, patch: Partial<CachedRecord>): Promise<void> {
  const rec = await withStore('recordings', 'readonly', s => s.get(id) as IDBRequest<CachedRecord | undefined>);
  if (!rec) throw new Error(`no record ${id} to patch`);
  await withStore('recordings', 'readwrite', s => s.put({ ...rec, ...patch }));
}

/** Overwrite the liveness lock map exactly as a crashed tab would have left it. */
function setLocks(locks: Record<string, number>): void {
  localStorage.setItem(LOCK_KEY, JSON.stringify(locks));
}

beforeEach(async () => { await freshModule(); });
afterEach(() => { vi.unstubAllGlobals(); });

describe('recording-cache — round trip', () => {
  it('create→append→finalize→listRestorable assembles one playable audio Blob with the title', async () => {
    await cache.createSession('s1', 'p1', 'd1');
    await cache.appendChunk('s1', new Blob(['aa'], { type: 'audio/webm' }));
    await cache.appendChunk('s1', new Blob(['bb'], { type: 'audio/webm' }));
    await cache.finalizeSession('s1', 'voice-2026-09-07 14:00.webm');

    const list = await cache.listRestorable();
    expect(list).toHaveLength(1);
    expect(list[0].id).toBe('s1');
    expect(list[0].status).toBe('pending-upload');
    expect(list[0].title).toBe('voice-2026-09-07 14:00.webm');
    expect(list[0].blob.type).toBe('audio/webm');
    expect(await list[0].blob.text()).toBe('aabb');
  });

  it('keeps chunk order across queued fire-and-forget appends', async () => {
    await cache.createSession('s1', 'p1', 'd1');
    const blobs = ['1', '2', '3', '4', '5'].map(s => new Blob([s]));
    // Not awaited in order — the module's queue must serialize them.
    blobs.forEach(b => void cache.appendChunk('s1', b));
    await cache.finalizeSession('s1', 't.webm');
    const drained = await cache.listRestorable();
    expect(await drained[0].blob.text()).toBe('12345');
  });
});

describe('recording-cache — liveness filter', () => {
  it('skips a stuck recording with a FRESH lock (live in another tab)', async () => {
    await cache.createSession('live', 'p1', 'd1');
    await cache.appendChunk('live', new Blob(['take']));

    expect(await cache.listRestorable()).toHaveLength(0);
  });

  it('restores a stuck recording once the lock is stale (crashed ≥15s ago)', async () => {
    await cache.createSession('s1', 'p1', 'd1');
    await cache.appendChunk('s1', new Blob(['take']));
    setLocks({ s1: Date.now() - 16_000 });

    const list = await cache.listRestorable();
    expect(list).toHaveLength(1);
    expect(list[0].status).toBe('recording');
  });

  it('restores a stuck recording with NO lock entry at once (clean close released it)', async () => {
    await cache.createSession('s1', 'p1', 'd1');
    await cache.appendChunk('s1', new Blob(['take']));
    setLocks({});

    expect(await cache.listRestorable()).toHaveLength(1);
  });

  it('without localStorage, falls back to record age for liveness', async () => {
    await cache.createSession('fresh', 'p1', 'd1');
    await cache.createSession('stale', 'p1', 'd1');
    await cache.appendChunk('fresh', new Blob(['a']));
    await cache.appendChunk('stale', new Blob(['b']));
    await patchRecord('stale', { updatedAt: Date.now() - 16_000 });

    vi.stubGlobal('localStorage', undefined);

    const list = await cache.listRestorable();
    expect(list.map(r => r.id)).toEqual(['stale']);
  });

  it('filters by project and skips zero-chunk records', async () => {
    await cache.createSession('mine', 'p1', 'd1');
    await cache.appendChunk('mine', new Blob(['take']));
    await cache.finalizeSession('mine', 't.webm');
    await cache.createSession('empty', 'p1', 'd1');
    await cache.appendChunk('other', new Blob(['take']));
    await cache.finalizeSession('other', 't.webm');

    const p1 = await cache.listRestorable('p1');
    expect(p1.map(r => r.id)).toEqual(['mine']);
  });

  it('releasing locks on pagehide makes a clean-closed take restorable at once', async () => {
    await cache.createSession('s1', 'p1', 'd1');
    await cache.appendChunk('s1', new Blob(['take']));
    expect(await cache.listRestorable()).toHaveLength(0);

    window.dispatchEvent(new Event('pagehide'));

    expect(await cache.listRestorable()).toHaveLength(1);
    expect(JSON.parse(localStorage.getItem(LOCK_KEY) ?? '{}')).not.toHaveProperty('s1');
  });
});

describe('recording-cache — deletion surfaces', () => {
  it('deleteSession drops one record and its lock', async () => {
    await cache.createSession('s1', 'p1', 'd1');
    await cache.appendChunk('s1', new Blob(['take']));
    await cache.finalizeSession('s1', 't.webm');

    await cache.deleteSession('s1');

    expect(await readAllRecords()).toHaveLength(0);
    expect(JSON.parse(localStorage.getItem(LOCK_KEY) ?? '{}')).not.toHaveProperty('s1');
  });

  it('deleteActiveSession refuses a still-recording session but drops the finished one', async () => {
    await cache.createSession('s1', 'p1', 'd1');
    await cache.appendChunk('s1', new Blob(['take']));

    await cache.deleteActiveSession();
    expect((await readAllRecords()).map(r => r.id)).toEqual(['s1']);

    await cache.finalizeSession('s1', 't.webm');
    await cache.deleteActiveSession();
    expect(await readAllRecords()).toHaveLength(0);
  });

  it('clearProjectRecordings drops only that project, clearRecordingsForDocument only that document', async () => {
    for (const [id, p, d] of [['a', 'p1', 'd1'], ['b', 'p1', 'd2'], ['c', 'p2', 'd3']] as const) {
      await cache.createSession(id, p, d);
      await cache.appendChunk(id, new Blob(['take']));
      await cache.finalizeSession(id, 't.webm');
    }

    await cache.clearProjectRecordings('p1');
    expect((await readAllRecords()).map(r => r.id)).toEqual(['c']);

    await cache.clearRecordingsForDocument('d3');
    expect(await readAllRecords()).toHaveLength(0);
  });

  it('clearAll empties everything (logout)', async () => {
    await cache.createSession('a', 'p1', 'd1');
    await cache.appendChunk('a', new Blob(['take']));
    await cache.finalizeSession('a', 't.webm');

    await cache.clearAll();

    expect(await readAllRecords()).toHaveLength(0);
    expect(cache.hasUnsent()).toBe(false);
    expect(localStorage.getItem(LOCK_KEY)).toBeNull();
  });
});

describe('recording-cache — boot sweep', () => {
  it('drops records older than 30d and stale zero-chunk records, keeps the rest', async () => {
    await cache.createSession('old', 'p1', 'd1');
    await cache.appendChunk('old', new Blob(['take']));
    await patchRecord('old', { createdAt: Date.now() - 31 * 24 * 60 * 60 * 1000 });

    await cache.createSession('empty', 'p1', 'd1');
    await patchRecord('empty', { createdAt: Date.now() - 60_000 });

    await cache.createSession('fresh', 'p1', 'd1');
    await cache.appendChunk('fresh', new Blob(['take']));

    await cache.sweepRecordingCache();

    expect((await readAllRecords()).map(r => r.id)).toEqual(['fresh']);
    expect(cache.hasUnsent()).toBe(true);
  });

  it('drops v1-shaped records (rows from the pre-chunk-store schema, no chunkCount)', async () => {
    // Seed a v1-shaped row directly: bytes lived inside the record, no chunkCount
    // field. v2 must treat it as zero-chunk and sweep it past the grace window.
    await withStore('recordings', 'readwrite', s => s.put({
      id: 'v1row', projectId: 'p1', documentId: 'd1',
      createdAt: Date.now() - 60_000, updatedAt: Date.now() - 60_000,
      status: 'pending-upload', title: 'old-take.webm', chunks: [new Uint8Array([1])],
    } as unknown as CachedRecord));

    await cache.sweepRecordingCache();

    expect((await readAllRecords()).map(r => r.id)).toEqual([]);
  });

  it('hydrates the unsent counter from the database a previous session left behind', async () => {
    await cache.createSession('s1', 'p1', 'd1');
    await cache.appendChunk('s1', new Blob(['take']));
    await cache.finalizeSession('s1', 't.webm');

    // Simulate a browser reopen: module reloaded, SAME database.
    vi.resetModules();
    cache = await import('./recording-cache');
    expect(cache.hasUnsent()).toBe(false); // hydration is async — not yet claimed

    await cache.listRestorable(); // queues behind the boot sweep → hydration done
    expect(cache.hasUnsent()).toBe(true);
  });
});

describe('recording-cache — chunk store durability (v2)', () => {
  it('N appends survive a module reload and come back in order', async () => {
    await cache.createSession('s1', 'p1', 'd1');
    for (const s of ['a', 'b', 'c', 'd']) await cache.appendChunk('s1', new Blob([s]));
    await cache.finalizeSession('s1', 't.webm');

    // Browser reopen: fresh module, SAME database — the durability contract the
    // old raw-record store could not express.
    vi.resetModules();
    cache = await import('./recording-cache');

    const list = await cache.listRestorable();
    expect(list).toHaveLength(1);
    expect(list[0].id).toBe('s1');
    expect(await list[0].blob.text()).toBe('abcd');
  });

  it('deleteSession leaves nothing that resurrects after a module reload (chunk rows too)', async () => {
    await cache.createSession('s1', 'p1', 'd1');
    await cache.appendChunk('s1', new Blob(['take']));
    await cache.finalizeSession('s1', 't.webm');

    await cache.deleteSession('s1');

    vi.resetModules();
    cache = await import('./recording-cache');
    await cache.listRestorable(); // queues behind the boot sweep → hydration done

    expect(await cache.listRestorable()).toHaveLength(0);
    expect(await readAllRecords()).toHaveLength(0);
    expect(await readChunkRows()).toHaveLength(0);
  });

  it('appendChunk writes chunk rows keyed (sessionId, seq) with the bumped count', async () => {
    await cache.createSession('s1', 'p1', 'd1');
    await cache.appendChunk('s1', new Blob(['ab']));
    await cache.appendChunk('s1', new Blob(['cd']));

    const rows = await readChunkRows();
    expect(rows.map(r => [r.sessionId, r.seq])).toEqual([['s1', 0], ['s1', 1]]);
    expect((await readAllRecords())[0].chunkCount).toBe(2);
  });
});

describe('recording-cache — synchronous counter', () => {
  it('flips on create/delete without awaiting anything', async () => {
    expect(cache.hasUnsent()).toBe(false);
    await cache.createSession('s1', 'p1', 'd1');
    expect(cache.hasUnsent()).toBe(true);
    await cache.deleteSession('s1');
    expect(cache.hasUnsent()).toBe(false);
  });
});

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

  it('memory fallback still assembles chunks in-session, in order, from the memory map', async () => {
    const real = globalThis.indexedDB;
    // @ts-expect-error — simulating a browser/private mode without IndexedDB
    delete globalThis.indexedDB;
    try {
      vi.resetModules();
      localStorage.clear();
      cache = await import('./recording-cache');

      await cache.createSession('s1', 'p1', 'd1');
      await cache.appendChunk('s1', new Blob(['ab']));
      await cache.appendChunk('s1', new Blob(['cd']));
      await cache.finalizeSession('s1', 't.webm');

      const list = await cache.listRestorable();
      expect(list).toHaveLength(1);
      expect(list[0].id).toBe('s1');
      expect(await list[0].blob.text()).toBe('abcd');
    } finally {
      globalThis.indexedDB = real;
    }
  });
});
