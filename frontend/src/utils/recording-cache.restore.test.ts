/**
 * Restore flow contract: a 2xx upload deletes the cached take, an auth failure
 * keeps it (re-login rescues), an access refusal deletes it and says so, an
 * outage keeps it and says so, and concurrent restores of one project dedupe.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import 'fake-indexeddb/auto';

const mocks = vi.hoisted(() => ({
  upload: vi.fn(),
  addReference: vi.fn(),
  replaceReference: vi.fn(),
  updateReference: vi.fn(),
  removeReference: vi.fn(),
  showToast: vi.fn(),
}));

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>();
  return { ...actual, apiClient: { ...actual.apiClient, upload: mocks.upload } };
});

vi.mock('../store/app-store', () => {
  const state = {
    addReference: mocks.addReference,
    replaceReference: mocks.replaceReference,
    updateReference: mocks.updateReference,
    removeReference: mocks.removeReference,
    showToast: mocks.showToast,
  };
  return {
    useAppStore: Object.assign(
      (selector: (s: typeof state) => unknown) => selector(state),
      { getState: () => state },
    ),
  };
});

vi.mock('../i18n', () => ({ t: (k: string) => k }));

const DB_NAME = 'lore-recording-cache';
const LOCK_KEY = 'lore-recording-live';

type Cache = typeof import('./recording-cache');

let cache: Cache;
let AuthErrorC: typeof import('../api/client').AuthError;
let HttpErrorC: typeof import('../api/client').HttpError;

async function freshModule(): Promise<void> {
  vi.resetModules();
  vi.clearAllMocks();
  localStorage.clear();
  await new Promise<void>((resolve) => {
    const req = indexedDB.deleteDatabase(DB_NAME);
    req.onsuccess = req.onerror = req.onblocked = () => resolve();
  });
  ({ AuthError: AuthErrorC, HttpError: HttpErrorC } = await import('../api/client'));
  cache = await import('./recording-cache');
}

async function seedPending(id: string, projectId = 'p1', documentId = 'd1', title = 'voice-t.webm'): Promise<void> {
  await cache.createSession(id, projectId, documentId);
  await cache.appendChunk(id, new Blob(['take'], { type: 'audio/webm' }));
  await cache.finalizeSession(id, title);
}

/** A crashed mid-take recording: status still `recording`, lock aged past the grace window. */
async function seedCrashed(id: string, projectId = 'p1'): Promise<void> {
  await cache.createSession(id, projectId, 'd1');
  await cache.appendChunk(id, new Blob(['partial'], { type: 'audio/webm' }));
  const locks = JSON.parse(localStorage.getItem(LOCK_KEY) ?? '{}');
  locks[id] = Date.now() - 16_000;
  localStorage.setItem(LOCK_KEY, JSON.stringify(locks));
}

beforeEach(async () => { await freshModule(); });
afterEach(() => { vi.clearAllMocks(); });

describe('restorePendingRecordings', () => {
  it('uploads a restorable take, replaces the temp ref, deletes the record; other projects untouched', async () => {
    await seedPending('s1');
    await seedPending('s2', 'p2', 'd9', 'other.webm');
    // Zero-chunk record (stop with no data ever arriving) — must be skipped.
    await cache.createSession('empty', 'p1', 'd1');
    await cache.finalizeSession('empty', 'voice-empty.webm');
    mocks.upload.mockResolvedValue({ reference_id: 'r1' });

    await cache.restorePendingRecordings('p1');

    expect(mocks.upload).toHaveBeenCalledTimes(1);
    const [endpoint, formData] = mocks.upload.mock.calls[0] as [string, FormData];
    expect(endpoint).toBe('/references/upload');
    expect(formData.get('project_id')).toBe('p1');
    expect(formData.get('document_id')).toBe('d1');
    expect(formData.get('title')).toBe('voice-t.webm');
    // The cache session id rides along as the upload idempotency key — a lost
    // 2xx ack re-sends the SAME key and must be served the SAME reference.
    expect(formData.get('idempotency_key')).toBe('s1');
    expect((formData.get('file') as File).name).toBe('voice-t.webm');

    expect(mocks.replaceReference).toHaveBeenCalledTimes(1);
    const [tempId, ref] = mocks.replaceReference.mock.calls[0] as [string, { reference_id: string }];
    expect(tempId).toMatch(/^temp_/);
    expect(ref.reference_id).toBe('r1');

    expect(await cache.listRestorable('p1')).toHaveLength(0);
    expect(await cache.listRestorable('p2')).toHaveLength(1);
  });

  it('a crash-truncated take is named a restored partial, a finished one an unsent upload', async () => {
    await seedCrashed('crash');
    await seedPending('done');
    mocks.upload.mockResolvedValue({ reference_id: 'r9' });

    await cache.restorePendingRecordings('p1');

    const kinds = mocks.showToast.mock.calls.map(c => c[0]);
    expect(kinds).toContain('recordingRestoredPartial');
    expect(kinds).toContain('recordingRestoredUploading');
    expect(mocks.upload).toHaveBeenCalledTimes(2);
  });

  it('a 2xx after a previous 5xx re-sends the kept take (no lock-in)', async () => {
    await seedPending('s1');
    mocks.upload.mockRejectedValueOnce(new TypeError('fetch failed'));
    await cache.restorePendingRecordings('p1');
    expect(await cache.listRestorable('p1')).toHaveLength(1);

    mocks.upload.mockResolvedValue({ reference_id: 'r2' });
    await cache.restorePendingRecordings('p1');
    expect(await cache.listRestorable('p1')).toHaveLength(0);
  });

  it('an auth failure KEEPS the record — a re-login rescues it', async () => {
    await seedPending('s1');
    mocks.upload.mockRejectedValue(new AuthErrorC());

    await cache.restorePendingRecordings('p1');

    expect(await cache.listRestorable('p1')).toHaveLength(1);
    expect(mocks.removeReference).not.toHaveBeenCalled();
  });

  it('an access refusal DELETES the record, removes the temp ref and toasts the failure', async () => {
    await seedPending('s1');
    mocks.upload.mockRejectedValue(new HttpErrorC(404, 'not found'));

    await cache.restorePendingRecordings('p1');

    expect(await cache.listRestorable('p1')).toHaveLength(0);
    expect(mocks.removeReference).toHaveBeenCalledTimes(1);
    expect(mocks.showToast).toHaveBeenCalledWith('recordingRestoreFailed', 'error');
  });

  it('a network failure KEEPS the record and toasts the retry promise', async () => {
    await seedPending('s1');
    mocks.upload.mockRejectedValue(new TypeError('fetch failed'));

    await cache.restorePendingRecordings('p1');

    expect(await cache.listRestorable('p1')).toHaveLength(1);
    expect(mocks.showToast).toHaveBeenCalledWith('recordingKeptForRetry', 'error');
    expect(mocks.updateReference).toHaveBeenCalledWith(expect.any(String), { processing_status: 'error' });
  });

  it('dedupes concurrent restores of one project (StrictMode double-invoke)', async () => {
    await seedPending('s1');
    mocks.upload.mockResolvedValue({ reference_id: 'r1' });

    await Promise.all([
      cache.restorePendingRecordings('p1'),
      cache.restorePendingRecordings('p1'),
    ]);

    expect(mocks.upload).toHaveBeenCalledTimes(1);
  });
});
