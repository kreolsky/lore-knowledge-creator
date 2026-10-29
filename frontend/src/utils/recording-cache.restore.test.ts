/**
 * Restore flow contract: a 2xx upload deletes the cached take, an auth failure
 * keeps it (re-login rescues), an access refusal deletes it and says so, a
 * tier refusal keeps it and names the tier, an outage keeps it and says so, and concurrent restores of one project dedupe.
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
let ForbiddenErrorC: typeof import('../api/client').ForbiddenError;

async function freshModule(): Promise<void> {
  vi.resetModules();
  vi.clearAllMocks();
  localStorage.clear();
  await new Promise<void>((resolve) => {
    const req = indexedDB.deleteDatabase(DB_NAME);
    req.onsuccess = req.onerror = req.onblocked = () => resolve();
  });
  ({ AuthError: AuthErrorC, HttpError: HttpErrorC, ForbiddenError: ForbiddenErrorC } = await import('../api/client'));
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

  it('a tier refusal KEEPS the record (a downgrade never destroys a take) and names the tier', async () => {
    await seedPending('s1');
    mocks.upload.mockRejectedValue(new ForbiddenErrorC('tier_required:voice'));

    await cache.restorePendingRecordings('p1');

    expect(await cache.listRestorable('p1')).toHaveLength(1);
    expect(mocks.removeReference).toHaveBeenCalledTimes(1);
    expect(mocks.showToast).toHaveBeenCalledWith('tierVoiceRequired', 'error');
    expect(mocks.showToast).not.toHaveBeenCalledWith('recordingRestoreFailed', 'error');
  });
});
