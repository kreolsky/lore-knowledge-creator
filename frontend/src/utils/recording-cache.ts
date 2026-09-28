/**
 * IndexedDB upload queue for voice recordings — a take survives a browser
 * close or crash and is re-sent when its project is next opened.
 */
// SYSTEM: recording-cache — IndexedDB upload queue for voice recordings
// ARCH: raw IndexedDB, no wrapper dependency: one DB `lore-recording-cache`
//       (v2), store `recordings` keyed by session id + store `recording-chunks`
//       keyed [sessionId, seq]. Chunks are persisted as they arrive (the 1s
//       MediaRecorder timeslice) into the chunk store — each append is O(1) in
//       bytes and count regardless of take length (the v1 whole-record rewrite
//       per append was O(n²): ~18.8 GB written per hour-long take) — and the
//       file is assembled only at read time. The backend repairs duration-less
//       WebM (backend/files_util.py), so a crash-truncated chunk stream
//       is still playable. The session id doubles as the upload
//       idempotency_key (replay-safe re-sends).
// WHY: every IndexedDB failure below logs only. The cache is a crash-safety copy;
//       the take itself uploads from memory when recording stops, and an upload
//       failure is toasted (recordingKeptForRetry).

import { useAppStore } from '../store/app-store';
import { apiClient, AuthError, isAccessRefusal } from '../api/client';
import { createTempRef } from '../components/references/ref-utils';
import { t } from '../i18n';
import { registerLogoutHandler } from '../store/logout-handlers';
import type { Project, Document, Reference } from '../types';

const DB_NAME = 'lore-recording-cache';
const DB_VERSION = 2;
const STORE = 'recordings';
const CHUNK_STORE = 'recording-chunks';
/** localStorage map `{ sessionId: lastHeartbeatMs }` — the multi-tab liveness lock. */
const LOCK_KEY = 'lore-recording-live';
/** Mirrors the doc mirrors' MAX_AGE: a cached take nobody came back for in 30d is dead. */
const MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000;
/** A `recording` lock/updatedAt younger than this is LIVE (another tab is rolling). */
const LIVE_GRACE_MS = 15_000;

type RecordingStatus = 'recording' | 'pending-upload';

interface CachedRecording {
  id: string;
  projectId: string;
  documentId: string;
  createdAt: number;
  updatedAt: number;
  status: RecordingStatus;
  title?: string;
  /** v2: count of chunk rows in `recording-chunks` for this session. A v1 row
   * (pre-chunk-store) carries no field and reads as 0 at runtime — the boot
   * sweep drops it as zero-chunk. */
  chunkCount: number;
}

/** One persisted chunk — compound key [sessionId, seq] gives chunk order. */
interface ChunkRow {
  sessionId: string;
  seq: number;
  // WHY Uint8Array, not Blob: chunk BYTES, not Blob objects — Node's
  // structuredClone (and therefore fake-indexeddb) does not recognize jsdom's
  // Blob, so a Blob part silently degrades to "[object Object]". The Blob is
  // assembled only at read time.
  data: Uint8Array<ArrayBuffer>;
}

export interface RestorableRecording {
  id: string;
  projectId: string;
  documentId: string;
  status: RecordingStatus;
  title?: string;
  createdAt: number;
  updatedAt: number;
  blob: Blob;
}

// ── module state ─────────────────────────────────────────────────────────────

/** INVARIANT: a record is deleted ONLY after a 2xx upload ack or a user discard
 *  (Escape / logout / access refusal). Why: the queue exists to outlive failed
 *  uploads — deleting on error would recreate the data loss it was built for. */

/** Memory fallback when IndexedDB is unusable (private mode, quota, corrupt store). */
const memoryRecords = new Map<string, CachedRecording>();
// INVARIANT: memory-mode record (chunkCount) and its bytes (memoryChunks) are
// mutated in ONE synchronous block. Why: the memory mirror of the single-tx
// rule in appendChunk — splitting them makes memory-mode takes read zero-chunk
// and silently stop restoring in-session.
const memoryChunks = new Map<string, Uint8Array<ArrayBuffer>[]>();
let memoryOnly = false;
let memoryOnlyReported = false;

/** Serializes every DB operation. The first link is the boot sweep, so mutations
 *  can never race the sweep or the counter hydration. */
let queue: Promise<unknown> = Promise.resolve();

/** Synchronous unsent-record count — an async read cannot answer beforeunload.
 *  Hydrated from IDB by the boot sweep, maintained on every mutation. */
let unsentCount = 0;

/** The session this tab last created — what deleteActiveSession() names. */
let activeId: string | null = null;
/** Every session this tab created — whose locks pagehide releases. */
const tabSessionIds = new Set<string>();

// ── IndexedDB plumbing ───────────────────────────────────────────────────────

function hasIndexedDb(): boolean {
  return typeof indexedDB !== 'undefined' && indexedDB !== null;
}

function reportMemoryOnly(err: unknown): void {
  memoryOnly = true;
  if (!memoryOnlyReported) {
    memoryOnlyReported = true;
    console.error('recording cache: IndexedDB unavailable, recordings will not survive a browser close', err);
  }
}

/** True when this session must serve from memory (unusable OR absent IndexedDB). */
function inMemoryMode(): boolean {
  if (memoryOnly) return true;
  if (!hasIndexedDb()) {
    reportMemoryOnly(new Error('indexedDB is not available'));
    return true;
  }
  return false;
}

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      if (!req.result.objectStoreNames.contains(STORE)) {
        req.result.createObjectStore(STORE, { keyPath: 'id' });
      }
      if (!req.result.objectStoreNames.contains(CHUNK_STORE)) {
        req.result.createObjectStore(CHUNK_STORE, { keyPath: ['sessionId', 'seq'] });
      }
      // No v1→v2 data migration: v1 records keep their raw `chunks` array and
      // lack `chunkCount` → the boot sweep drops them as zero-chunk (the cache
      // shipped one commit before this store; only dev machines held v1 rows).
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error ?? new Error('indexedDB open failed'));
    req.onblocked = () => reject(new Error('indexedDB open blocked'));
  });
}

/**
 * WHY: a fresh connection per operation, closed in `finally`, instead of one
 * long-lived handle. Why: an open connection blocks `deleteDatabase` forever
 * (this module never deletes the database, but a browser reclaiming quota, a
 * future cleanup path, or a second tab's sweep may) — and the connection cost
 * of a 1s-cadence append queue is nothing.
 */
async function getDb(): Promise<IDBDatabase> {
  try {
    return await openDb();
  } catch (e) {
    reportMemoryOnly(e);
    throw e;
  }
}

function reqToPromise<T>(req: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function withDb<T>(
  store: string,
  mode: IDBTransactionMode,
  fn: (s: IDBObjectStore) => IDBRequest<T>,
): Promise<T> {
  const d = await getDb();
  try {
    return await reqToPromise(fn(d.transaction(store, mode).objectStore(store)));
  } finally {
    d.close();
  }
}

/** Full key range of one session's chunk rows. WHY integer bounds, not
 *  ±Infinity: Infinity is not a valid IDB key (DataError in browsers); seq is
 *  a non-negative integer, so [sid, 0]..[sid, MAX_SAFE_INTEGER] is the whole
 *  sid range and compound-key ordering returns rows in chunk order. */
function sidRange(sessionId: string): IDBKeyRange {
  return IDBKeyRange.bound([sessionId, 0], [sessionId, Number.MAX_SAFE_INTEGER]);
}

/** Run fn inside ONE transaction spanning both stores; resolves on completion.
 * The single place a record and its chunk rows are mutated together — see the
 * INVARIANT in appendChunk. */
function withBothStores(
  mode: IDBTransactionMode,
  fn: (records: IDBObjectStore, chunks: IDBObjectStore) => void,
): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    void getDb().then(d => {
      let tx: IDBTransaction;
      try {
        tx = d.transaction([STORE, CHUNK_STORE], mode);
        fn(tx.objectStore(STORE), tx.objectStore(CHUNK_STORE));
      } catch (e) {
        d.close();
        reject(e);
        return;
      }
      tx.oncomplete = () => { d.close(); resolve(); };
      tx.onerror = () => { d.close(); reject(tx.error ?? new Error('transaction failed')); };
      tx.onabort = () => { d.close(); reject(tx.error ?? new Error('transaction aborted')); };
    }, reject);
  });
}

function idbGet(id: string): Promise<CachedRecording | undefined> {
  return withDb(STORE, 'readonly', s => s.get(id)) as Promise<CachedRecording | undefined>;
}

function idbGetAll(): Promise<CachedRecording[]> {
  return withDb(STORE, 'readonly', s => s.getAll()) as Promise<CachedRecording[]>;
}

function idbPut(rec: CachedRecording): Promise<void> {
  return withDb(STORE, 'readwrite', s => s.put(rec)).then(() => undefined);
}

function idbGetChunks(sessionId: string): Promise<ChunkRow[]> {
  return withDb(CHUNK_STORE, 'readonly', s => s.getAll(sidRange(sessionId))) as Promise<ChunkRow[]>;
}

/** Every public op runs on the queue, in call order, and never rejects — the
 *  cache is best-effort storage (same contract as SYSTEM: local-doc-persistence). */
function enqueue<T>(fn: () => Promise<T>): Promise<T> {
  const p = queue.then(fn);
  queue = p.then(() => undefined, () => undefined);
  return p;
}

// ── liveness locks (localStorage) ────────────────────────────────────────────

/** null = localStorage unusable → the caller falls back to record age. */
function readLocks(): Record<string, number> | null {
  try {
    const raw = localStorage.getItem(LOCK_KEY);
    if (raw === null) return {};
    const parsed: unknown = JSON.parse(raw);
    return parsed !== null && typeof parsed === 'object' ? (parsed as Record<string, number>) : {};
  } catch {
    return null;
  }
}

function writeLock(id: string, ts: number): void {
  try {
    const locks = readLocks() ?? {};
    locks[id] = ts;
    localStorage.setItem(LOCK_KEY, JSON.stringify(locks));
  } catch { /* degraded: updatedAt carries liveness alone */ }
}

function clearLock(id: string): void {
  try {
    const locks = readLocks();
    if (locks === null || !(id in locks)) return;
    delete locks[id];
    localStorage.setItem(LOCK_KEY, JSON.stringify(locks));
  } catch { /* nothing to release through */ }
}

// ── public storage API ───────────────────────────────────────────────────────

/**
 * Open a cached session. Call ONLY after `recorder.start()` succeeded, in the
 * same synchronous block — a getUserMedia/MediaRecorder failure must leave no
 * empty record, and the first `ondataavailable` (~1s later) can never overtake
 * the queued create.
 */
export function createSession(id: string, projectId: string, documentId: string): Promise<void> {
  return enqueue(async () => {
    activeId = id;
    tabSessionIds.add(id);
    const now = Date.now();
    const rec: CachedRecording = { id, projectId, documentId, createdAt: now, updatedAt: now, status: 'recording', chunkCount: 0 };
    if (inMemoryMode()) {
      memoryRecords.set(id, rec);
      memoryChunks.set(id, []);
      unsentCount += 1;
      return;
    }
    try {
      await idbPut(rec);
      unsentCount += 1;
    } catch (e) {
      console.error('recording cache: createSession failed', e);
    }
  });
}

/** Persist one chunk as it arrives; also the liveness heartbeat. Fire-and-forget. */
export function appendChunk(id: string, chunk: Blob): Promise<void> {
  return enqueue(async () => {
    writeLock(id, Date.now());
    const data = new Uint8Array(await chunk.arrayBuffer());
    if (inMemoryMode()) {
      const rec = memoryRecords.get(id);
      if (rec) {
        const arr = memoryChunks.get(id);
        if (arr) arr.push(data);
        else memoryChunks.set(id, [data]);
        rec.chunkCount += 1;
        rec.updatedAt = Date.now();
      }
      return;
    }
    try {
      // INVARIANT(data-loss): the chunk row (`add`, seq = current count) and
      // the bumped session record share ONE transaction. Why: count and rows
      // must never drift — a stale count would overwrite an existing chunk
      // row = an audio gap. A later "optimization" splitting these two writes
      // reintroduces silent chunk loss; `add` (not put) makes a seq collision
      // abort loudly.
      await withBothStores('readwrite', (records, chunks) => {
        const getReq = records.get(id);
        getReq.onsuccess = () => {
          const rec = getReq.result as CachedRecording | undefined;
          if (!rec) return; // session already discarded — drop the orphan chunk
          chunks.add({ sessionId: id, seq: rec.chunkCount, data } satisfies ChunkRow);
          records.put({ ...rec, chunkCount: rec.chunkCount + 1, updatedAt: Date.now() });
        };
      });
    } catch (e) {
      console.error('recording cache: appendChunk failed', e);
    }
  });
}

/** The take is complete but unsent → restorable regardless of liveness. */
export function finalizeSession(id: string, title: string): Promise<void> {
  return enqueue(async () => {
    clearLock(id);
    if (inMemoryMode()) {
      const rec = memoryRecords.get(id);
      if (rec) {
        rec.status = 'pending-upload';
        rec.title = title;
        rec.updatedAt = Date.now();
      }
      return;
    }
    try {
      const rec = await idbGet(id);
      if (!rec) return;
      rec.status = 'pending-upload';
      rec.title = title;
      rec.updatedAt = Date.now();
      await idbPut(rec);
    } catch (e) {
      console.error('recording cache: finalizeSession failed', e);
    }
  });
}

/** User discard (Escape) or explicit cleanup — deletes regardless of status. */
export function deleteSession(id: string): Promise<void> {
  return enqueue(() => doDelete(id));
}

async function doDelete(id: string): Promise<void> {
  tabSessionIds.delete(id);
  clearLock(id);
  if (activeId === id) activeId = null;
  if (inMemoryMode()) {
    if (memoryRecords.delete(id)) unsentCount -= 1;
    memoryChunks.delete(id);
    return;
  }
  try {
    let existed = false;
    // One transaction clears the session's chunk key-range AND its record —
    // a delete split across stores could leave chunk rows that resurrect as a
    // zero-record blob on the next read path change.
    await withBothStores('readwrite', (records, chunks) => {
      const getReq = records.get(id);
      getReq.onsuccess = () => {
        if (getReq.result === undefined) return;
        existed = true;
        records.delete(id);
        chunks.delete(sidRange(id));
      };
    });
    if (existed) unsentCount -= 1;
  } catch (e) {
    console.error('recording cache: deleteSession failed', e);
  }
}

/**
 * Delete the tab's current session — how the completion flows (useVoiceInput,
 * ProjectPage) drop the cache after a 2xx without knowing the session id.
 *
 * INVARIANT: never deletes a session that is still `recording`. Why: a new take
 * may already be rolling by the time an older upload's 2xx lands, and deleting
 * THAT session mid-recording would silently kill its cache; a `recording`
 * session is only ever dropped by an explicit user discard (deleteSession from
 * the Escape path).
 */
export function deleteActiveSession(): Promise<void> {
  return enqueue(async () => {
    if (activeId === null) return;
    const rec = memoryOnly ? memoryRecords.get(activeId) : await idbGet(activeId).catch(() => undefined);
    if (!rec || rec.status === 'recording') return;
    await doDelete(activeId);
  });
}

/**
 * Restorable takes: `pending-upload` always; a stuck `recording` only when NOT
 * live — no lock entry (clean close released it), or a lock/updatedAt older
 * than the grace window (crashed). Zero-chunk records never restore.
 */
export function listRestorable(projectId?: string): Promise<RestorableRecording[]> {
  return enqueue(async () => {
    let all: CachedRecording[];
    if (inMemoryMode()) {
      all = Array.from(memoryRecords.values());
    } else {
      try {
        all = await idbGetAll();
      } catch (e) {
        console.error('recording cache: listRestorable failed', e);
        return [];
      }
    }
    const now = Date.now();
    const locks = readLocks();
    const result: RestorableRecording[] = [];
    for (const r of all
      .filter(r => projectId === undefined || r.projectId === projectId)
      .filter(r => (r.chunkCount ?? 0) > 0) // ?? : v1 rows carry no field
      .filter(r => r.status === 'pending-upload' || isNotLive(r, locks, now))) {
      const byteArrays: Uint8Array<ArrayBuffer>[] | null = inMemoryMode()
        ? (memoryChunks.get(r.id) ?? [])
        : await idbGetChunks(r.id)
            .then(rows => rows.map(row => row.data))
            .catch(() => null);
      if (byteArrays === null) continue; // chunks unreadable — skip, never upload a silent partial
      result.push({
        id: r.id,
        projectId: r.projectId,
        documentId: r.documentId,
        status: r.status,
        title: r.title,
        createdAt: r.createdAt,
        updatedAt: r.updatedAt,
        blob: new Blob(byteArrays, { type: 'audio/webm' }),
      });
    }
    return result;
  });
}

function isNotLive(r: CachedRecording, locks: Record<string, number> | null, now: number): boolean {
  if (locks === null) return now - r.updatedAt >= LIVE_GRACE_MS;
  const ts = locks[r.id];
  if (ts === undefined) return true; // clean close — pagehide released it
  return now - ts >= LIVE_GRACE_MS; // crash — live only inside the grace window
}

/** Synchronous: is there anything unsent in the cache? Answers beforeunload. */
export function hasUnsent(): boolean {
  return unsentCount > 0;
}

/** Drop dead weight once per boot: records older than MAX_AGE, and zero-chunk
 *  records past the liveness grace (a live take's first chunk may simply not
 *  have arrived yet). Also hydrates the unsent counter. */
export function sweepRecordingCache(): Promise<void> {
  return enqueue(async () => {
    let all: CachedRecording[];
    if (inMemoryMode()) {
      all = Array.from(memoryRecords.values());
    } else {
      try {
        all = await idbGetAll();
      } catch (e) {
        console.error('recording cache: sweep failed', e);
        return;
      }
    }
    const now = Date.now();
    const cutoff = now - MAX_AGE_MS;
    const dead = all.filter(r => r.createdAt < cutoff
      || ((r.chunkCount ?? 0) === 0 && now - r.createdAt > LIVE_GRACE_MS)); // ?? : v1 rows carry no field
    for (const r of dead) await doDelete(r.id);
    unsentCount = all.length - dead.length;
  });
}

/** All of one project's cached takes (access refusal on the project). */
export function clearProjectRecordings(projectId: string): Promise<void> {
  return enqueue(() => clearMatching(r => r.projectId === projectId));
}

/** All takes of one document, project unknown (bare /docs/:id refusal). */
export function clearRecordingsForDocument(documentId: string): Promise<void> {
  return enqueue(() => clearMatching(r => r.documentId === documentId));
}

/** Everything — logout: a shared machine keeps no previous user's audio. */
export function clearAll(): Promise<void> {
  return enqueue(async () => {
    await clearMatching(() => true);
    try {
      localStorage.removeItem(LOCK_KEY);
    } catch { /* no locks to drop */ }
  });
}

async function clearMatching(matches: (r: CachedRecording) => boolean): Promise<void> {
  let all: CachedRecording[];
  if (inMemoryMode()) {
    all = Array.from(memoryRecords.values());
  } else {
    try {
      all = await idbGetAll();
    } catch (e) {
      console.error('recording cache: clear failed', e);
      return;
    }
  }
  for (const r of all.filter(matches)) await doDelete(r.id);
}

// ── boot restore ─────────────────────────────────────────────────────────────

const restoreInFlight = new Map<string, Promise<void>>();

/**
 * Re-send this project's restorable takes through the async upload path —
 * including former Cmd+D widget recordings, whose inline position is
 * unrecoverable. Deduped by an in-flight promise (StrictMode double-invoke).
 */
export function restorePendingRecordings(projectId: string): Promise<void> {
  const existing = restoreInFlight.get(projectId);
  if (existing) return existing;
  const p = doRestore(projectId).finally(() => { restoreInFlight.delete(projectId); });
  restoreInFlight.set(projectId, p);
  return p;
}

async function doRestore(projectId: string): Promise<void> {
  let restorable: RestorableRecording[];
  try {
    restorable = await listRestorable(projectId);
  } catch {
    return; // IDB gone — nothing to restore from
  }
  for (const r of restorable) {
    // A crash-truncated take is named for what it is; the title carries the timestamp.
    useAppStore.getState().showToast(
      t(r.status === 'recording' ? 'recordingRestoredPartial' : 'recordingRestoredUploading'),
      'info',
    );
    const title = r.title ?? `voice-${new Date(r.createdAt).toISOString()}.webm`;
    const { tempId, tempRef } = createTempRef(
      { project_id: r.projectId } as Project,
      { document_id: r.documentId } as Document,
      title,
      'audio',
    );
    useAppStore.getState().addReference(tempRef);

    const formData = new FormData();
    formData.append('file', new File([r.blob], title, { type: 'audio/webm' }));
    formData.append('project_id', r.projectId);
    formData.append('document_id', r.documentId);
    formData.append('title', title);
    // The record id IS the cache session id — the replay of a lost 2xx is
    // served the same reference instead of creating a duplicate.
    formData.append('idempotency_key', r.id);
    try {
      const ref = await apiClient.upload('/references/upload', formData);
      useAppStore.getState().replaceReference(tempId, ref as Reference);
      await deleteSession(r.id);
    } catch (err) {
      if (err instanceof AuthError) {
        // Session gone (apiClient already redirected) — keep: re-login rescues it.
        useAppStore.getState().updateReference(tempId, { processing_status: 'error' });
        continue;
      }
      if (isAccessRefusal(err)) {
        // The project/document refuses this user — a retry would never succeed.
        useAppStore.getState().removeReference(tempId);
        await deleteSession(r.id);
        useAppStore.getState().showToast(t('recordingRestoreFailed'), 'error');
        continue;
      }
      // Outage — the cache's whole reason to exist.
      useAppStore.getState().updateReference(tempId, { processing_status: 'error' });
      useAppStore.getState().showToast(t('recordingKeptForRetry'), 'error');
    }
  }
}

// ── lifecycle wiring ─────────────────────────────────────────────────────────

if (typeof window !== 'undefined') {
  // Clean close: release this tab's locks so a reopen restores the take at once
  // instead of waiting out the 15s crash grace.
  window.addEventListener('pagehide', () => {
    for (const id of tabSessionIds) clearLock(id);
  });
}

// INVARIANT(security): a soft logout drops every cached take. Why: the cache is
// the previous user's voice at rest on the machine; on a shared profile the next
// user must not have it re-uploaded into their session.
registerLogoutHandler(() => { void clearAll(); });

// Boot: sweep dead records and hydrate the unsent counter BEFORE any mutation
// (first link on the queue).
void sweepRecordingCache();
