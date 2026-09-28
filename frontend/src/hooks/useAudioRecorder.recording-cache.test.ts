/**
 * Tests for useAudioRecorder's wiring into the recording cache: chunks are
 * persisted while the take is rolling, stop finalizes to pending-upload,
 * Escape discards the cached session WITHOUT consuming the one-shot cancel
 * flag, and the in-memory File still reaches _onComplete.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import 'fake-indexeddb/auto';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const DB_NAME = 'lore-recording-cache';

type Cache = typeof import('../utils/recording-cache');
type CancelFlag = typeof import('../components/editor/voice-cancel-flag');

interface CachedRecord { id: string; status: string; title?: string; chunkCount: number }

let cache: Cache;
let cancelFlag: CancelFlag;
let useAudioRecorder: typeof import('./useAudioRecorder').useAudioRecorder;

let container: HTMLDivElement;
let root: Root;
let handlers: Record<string, (...args: unknown[]) => void>;
let onComplete: ReturnType<typeof vi.fn>;
let showToast: ReturnType<typeof vi.fn>;

/**
 * Real timers, a fast timeslice: the fake recorder honors the 1s API shape but
 * emits every 25ms, so tests need no fake-clock dance with IndexedDB's own
 * async event loop.
 */
class FakeMediaRecorder {
  static instances: FakeMediaRecorder[] = [];
  state: 'inactive' | 'recording' = 'inactive';
  ondataavailable: ((e: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;
  stream: { getTracks: () => { stop: () => void }[] };
  private timer: ReturnType<typeof setInterval> | null = null;

  constructor(stream: { getTracks: () => { stop: () => void }[] }) {
    this.stream = stream;
    FakeMediaRecorder.instances.push(this);
  }

  start(timeslice: number): void {
    this.state = 'recording';
    this.timer = setInterval(() => {
      this.ondataavailable?.({ data: new Blob(['chunk']) });
    }, Math.min(timeslice, 25));
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.state = 'inactive';
    this.onstop?.();
  }
}

async function readAllRecords(): Promise<CachedRecord[]> {
  const db = await new Promise<IDBDatabase>((resolve, reject) => {
    // No version pin: the module owns the DB version (v2 since the chunk store).
    const req = indexedDB.open(DB_NAME);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
  try {
    return await new Promise<CachedRecord[]>((resolve, reject) => {
      const tx = db.transaction('recordings', 'readonly');
      const req = tx.objectStore('recordings').getAll();
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  } finally {
    db.close();
  }
}

/**
 * Poll the store until the predicate holds over the records (fire-and-forget ops drain async).
 *
 * WHY ms=12_000 > the old 5_000 default: the deadline must sit WELL inside the
 * per-test timeout (15_000, see the it() calls) — with the old 5_000/5_000 pair
 * the vitest timer fired first and turned any CI-runner scheduling stall into an
 * opaque "Test timed out" (run #1246: the delete landed late once on a runner
 * that had just finished Docker Build; same code passed #1245 and 60+ local
 * replays). The module's queue guarantees ordering, not latency — a generous
 * wall-clock budget is tolerance, not masking: a real regression still surfaces
 * as this helper's descriptive "records never reached the expected state".
 */
async function untilRecords(pred: (recs: CachedRecord[]) => boolean, ms = 12_000): Promise<CachedRecord[]> {
  const deadline = Date.now() + ms;
  for (;;) {
    const recs = await readAllRecords();
    if (pred(recs)) return recs;
    if (Date.now() > deadline) throw new Error(`records never reached the expected state: ${JSON.stringify(recs)}`);
    await new Promise(r => setTimeout(r, 10));
  }
}

/**
 * Per-test DB teardown — resolves ONLY on real completion.
 *
 * WHY not `onsuccess = onerror = onblocked = resolve` (the old shape): on a
 * loaded CI runner the previous test's last in-flight cache op can still hold
 * an open connection when this runs, and fake-indexeddb then fires `blocked`
 * and KEEPS THE DELETE PENDING. Resolving on `blocked` handed the next test a
 * dirty DB — run #1283's Escape test then polled `recs.length === 0` against
 * the previous take's leftover `pending-upload` record for its whole deadline
 * (the failure dump's timestamps and title belonged to an earlier test in this
 * file, not to the test that failed). `blocked` therefore keeps this promise
 * pending: the delete settles when the blocking connection closes, and one
 * that never closes fails the test loudly (the it() timeout) instead of
 * leaking a dirty DB forward.
 */
function deleteDbForTest(): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    const req = indexedDB.deleteDatabase(DB_NAME);
    req.onsuccess = () => resolve();
    req.onerror = () => reject(req.error ?? new Error('deleteDatabase failed'));
    req.onblocked = () => { /* still pending on an open connection — wait */ };
  });
}

function sleep(ms: number): Promise<void> {
  return new Promise(r => setTimeout(r, ms));
}

beforeEach(async () => {
  vi.resetModules();
  localStorage.clear();
  await deleteDbForTest();
  FakeMediaRecorder.instances = [];
  handlers = {};
  onComplete = vi.fn();
  showToast = vi.fn();

  const appState = {
    currentProject: { project_id: 'p1', voice_recording_doc_id: null },
    currentDocument: { document_id: 'd1', title: 'Doc' },
    accessLevel: 'full',
    documents: [{ document_id: 'd1', title: 'Doc' }],
    setRecording: vi.fn(),
    showToast,
  };
  vi.doMock('../store/app-store', () => ({
    useAppStore: Object.assign(
      (selector: (s: typeof appState) => unknown) => selector(appState),
      { getState: () => appState },
    ),
  }));
  vi.doMock('./useEvent', () => ({
    useEvent: (name: string, cb: (...args: unknown[]) => void) => { handlers[name] = cb; },
  }));

  vi.stubGlobal('MediaRecorder', FakeMediaRecorder);
  Object.defineProperty(navigator, 'mediaDevices', {
    value: { getUserMedia: async () => ({ getTracks: () => [{ stop: vi.fn() }] }) },
    configurable: true,
  });

  ({ useAudioRecorder } = await import('./useAudioRecorder'));
  cache = await import('../utils/recording-cache');
  cancelFlag = await import('../components/editor/voice-cancel-flag');

  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => { try { root.unmount(); } catch { /* already unmounted */ } });
  container.remove();
  vi.unstubAllGlobals();
  vi.doUnmock('../store/app-store');
  vi.doUnmock('./useEvent');
});

function Harness() {
  useAudioRecorder(onComplete as unknown as (file: File, ctx: { projectId: string; documentId: string; sessionId: string }) => void);
  return null;
}

async function startRecording(): Promise<void> {
  await act(async () => { await (handlers['start-recording'] as () => Promise<void>)(); });
  await untilRecords(recs => recs.some(r => r.status === 'recording'));
}

// Per-test wall-clock budget: 15_000 keeps every untilRecords deadline (12_000,
// see above) reachable inside the test — see the comment there for why these
// IO-polled tests get more headroom than the 5_000 vitest default.
describe('useAudioRecorder → recording cache', () => {
  it('persists chunks while rolling; stop finalizes the session to pending-upload', async () => {
    await act(async () => { root.render(createElement(Harness)); });
    await startRecording();
    await sleep(80); // a few 25ms chunks
    const mid = await untilRecords(recs => (recs[0]?.chunkCount ?? 0) >= 2 && recs[0].status === 'recording');
    expect(mid).toHaveLength(1);

    await act(async () => { (handlers['stop-recording'] as () => void)(); });
    const done = await untilRecords(recs => recs[0]?.status === 'pending-upload');
    expect(done).toHaveLength(1);
    expect(done[0].title).toMatch(/^voice-.*\.webm$/);
  }, 15_000);

  it('still hands the in-memory File to _onComplete', async () => {
    await act(async () => { root.render(createElement(Harness)); });
    await startRecording();
    await sleep(60);
    await act(async () => { (handlers['stop-recording'] as () => void)(); });

    expect(onComplete).toHaveBeenCalledTimes(1);
    const [file, ctx] = onComplete.mock.calls[0] as [File, { projectId: string; documentId: string; sessionId: string }];
    expect(file).toBeInstanceOf(File);
    expect(file.name).toMatch(/^voice-.*\.webm$/);
    expect(file.size).toBeGreaterThan(0);
    expect(ctx).toMatchObject({ projectId: 'p1', documentId: 'd1' });
    // The ctx carries THIS take's cache session id — the idempotency key the
    // upload consumers append must be captured per take, never read from a
    // global (an await in the completion path would mis-key under a new take).
    const recs = await readAllRecords();
    expect(recs.some(r => r.id === ctx.sessionId)).toBe(true);
  }, 15_000);

  it('Escape discards the cached session AND the consuming isVoiceCancelled() still reads true', async () => {
    await act(async () => { root.render(createElement(Harness)); });
    await startRecording();
    await sleep(60);

    await act(async () => {
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    });
    await untilRecords(recs => recs.length === 0);

    // The peek in onstop must not have stolen the one consuming read that
    // belongs to handleVoiceRecording — losing it would resurrect the widget
    // sync-upload of a discarded take.
    expect(cancelFlag.isVoiceCancelled()).toBe(true);
    expect(cancelFlag.isVoiceCancelled()).toBe(false); // consumed exactly once
  }, 15_000);

  it('a failed getUserMedia leaves no empty record behind', async () => {
    Object.defineProperty(navigator, 'mediaDevices', {
      value: { getUserMedia: async () => { throw new Error('denied'); } },
      configurable: true,
    });
    await act(async () => { root.render(createElement(Harness)); });
    await act(async () => { await (handlers['start-recording'] as () => Promise<void>)(); });
    await sleep(50);

    expect(await readAllRecords()).toHaveLength(0);
    expect(cache.hasUnsent()).toBe(false);
    const { t } = await import('../i18n');
    expect(showToast).toHaveBeenCalledWith(t('recordingStartFailed'), 'error');
  }, 15_000);
});
